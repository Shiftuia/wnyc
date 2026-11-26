"""Deterministic core: geocoding, zone scoring (research/02), deserts, and the PNG map.

Everything the agent and the UI show comes from here, so the same question always gives the same answer.
"""
from __future__ import annotations

import io
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

import certifi
import duckdb

_SSL = ssl.create_default_context(cafile=certifi.where())  # python.org builds ship without system CAs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "prepared.duckdb"
# writable caches (answers, map PNGs, geocode/route lookups); the container mounts DATA read-only
CACHE = Path(os.environ.get("WNYC_CACHE_DIR") or DATA / "cache")
FT_800M = 800 / 0.3048
WORKDAYS = 22     # commuting days a month, for the plan-B cost estimate
MIN_UNITS = 1000  # zones with fewer multifamily units (parks, airports, industrial) are not ranked

WEIGHTS = {
    "housing": {"max": 30, "label": "Housing stock",
                "how": "15 × city percentile of units in post-1991 buildings (Fair Housing Act proxy) + 15 × city percentile of units in elevator buildings"},
    "transit": {"max": 30, "label": "Accessible subway",
                "how": "20 for a one-seat accessible ride to work (10 if one accessible transfer), + up to 10 for home-station elevator uptime (80% → 0, 100% → 10)"},
    "street": {"max": 15, "label": "Curb ramps",
               "how": "15 × city percentile of curb ramps whose detectable warning surface is in good condition"},
    "plan_b": {"max": 15, "label": "Plan B: accessible ride-hail",
               "how": "15 × city percentile of median wheelchair-accessible Uber/Lyft pickup wait (shorter = better)"},
    "rent": {"max": 10, "label": "Rent headroom",
             "how": "10 × (budget − typical rent) / budget, capped at 40% headroom"},
}

HUBS = [
    {"name": "350 5th Ave (Empire State Building)", "lat": 40.7484, "lon": -73.9857,
     "aliases": ["350 5th", "350 fifth", "empire state"]},
    {"name": "Midtown Center (Grand Central)", "lat": 40.7527, "lon": -73.9772,
     "aliases": ["midtown center", "grand central", "midtown"]},
    {"name": "Times Square", "lat": 40.7580, "lon": -73.9855, "aliases": ["times sq"]},
    {"name": "Financial District (Wall St)", "lat": 40.7075, "lon": -74.0113,
     "aliases": ["financial district", "fidi", "wall st", "wall street"]},
    {"name": "Downtown Brooklyn (MetroTech)", "lat": 40.6930, "lon": -73.9872,
     "aliases": ["downtown brooklyn", "metrotech", "borough hall"]},
    {"name": "Long Island City (Court Sq)", "lat": 40.7471, "lon": -73.9456,
     "aliases": ["long island city", "lic", "court sq"]},
    {"name": "Hudson Yards", "lat": 40.7540, "lon": -74.0020, "aliases": ["hudson yards"]},
    {"name": "Columbia University (Morningside Heights)", "lat": 40.8075, "lon": -73.9626,
     "aliases": ["columbia", "morningside"]},
    {"name": "Jamaica Center", "lat": 40.7023, "lon": -73.8010, "aliases": ["jamaica"]},
    {"name": "Bronx Hub (3 Av-149 St)", "lat": 40.8162, "lon": -73.9178,
     "aliases": ["bronx hub", "the hub", "3 av-149", "149 st"]},
    {"name": "Flushing (Main St)", "lat": 40.7596, "lon": -73.8301, "aliases": ["flushing"]},
    {"name": "St George (Staten Island Ferry)", "lat": 40.6437, "lon": -74.0736,
     "aliases": ["st george", "staten island ferry"]},
]

_con_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None


def con() -> duckdb.DuckDBPyConnection:
    """Read-only connection to the prepared tables (one cursor per call)."""
    global _con
    with _con_lock:
        if _con is None:
            if not DB_PATH.exists():
                raise RuntimeError("data/prepared.duckdb is missing — run: .venv/bin/python app/build_data.py")
            c = duckdb.connect(str(DB_PATH), read_only=True)
            c.execute("LOAD spatial")
            # the model writes its own SQL (run_sql) and the question comes from the public: no file or network
            # access beyond the already-open database, bounded memory/threads, and none of it can be switched back
            c.execute("SET enable_external_access = false")
            c.execute("SET memory_limit = '512MB'")
            c.execute("SET threads = 1")
            c.execute("SET max_temp_directory_size = '0KiB'")
            c.execute("SET lock_configuration = true")
            _con = c
        return _con.cursor()


def q(sql: str, params: list | None = None) -> list[dict]:
    cur = con()
    cur.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------- run_sql
# Model-authored SQL is untrusted (the question comes from the public and can carry prompt injection).
# Layers: one statement; SELECT/WITH/DESCRIBE/SUMMARIZE only; a keyword/function denylist on the text and on
# DuckDB's own parse tree; every table it reads must be one of the prepared tables (or a CTE of the query);
# table functions only range/generate_series/unnest; a read-only connection with external access off; a time
# limit (the query is interrupted); and caps on rows, cell length and total result size.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|pragma|set|reset|"
    r"call|checkpoint|vacuum|begin|commit|rollback|use|prepare|execute|"
    r"getenv|current_setting|which_secret|glob|httpfs|sniff_csv|query|query_table|"
    r"read_\w+|\w+_scan|parquet_\w+|st_read\w*|duckdb_\w+|pragma_\w+|sqlite_\w+|postgres_\w+|mysql_\w+|"
    r"iceberg_\w+|delta_\w+|json_execute_serialized_sql|from_json_serialized_sql|"
    r"information_schema|pg_catalog|pg_\w+|sqlite_master|sqlite_schema|temp|system)\b", re.I)
# string literals that look like paths or URLs are refused too (a replacement scan turns 'x.csv' into a file read)
_PATHLIKE = re.compile(r"'[^']*(?:/|\\|\.(?:csv|tsv|parquet|json|ndjson|txt|db|duckdb|sqlite|gz|zst)\b|://)[^']*'", re.I)
SQL_TIMEOUT_S = float(os.environ.get("WNYC_SQL_TIMEOUT_S", "5"))
MAX_CELL_CHARS = 300
MAX_RESULT_CHARS = 20000


@lru_cache(maxsize=1)
def allowed_tables() -> frozenset[str]:
    """The prepared tables and views in the main schema: the only relations model SQL may read."""
    return frozenset(r["n"].lower() for r in q(
        "SELECT table_name AS n FROM information_schema.tables WHERE table_schema = 'main'"))


def _check_sql(s: str) -> str | None:
    if ";" in s:
        return "one statement at a time"
    if len(s) > 4000:
        return "query too long (4,000 characters at most)"
    if not re.match(r"^\s*(select|with|describe|summarize|from)\b", s, re.I):
        return "only SELECT / WITH / DESCRIBE / SUMMARIZE are allowed"
    if _FORBIDDEN.search(s):
        return "statement uses a keyword or function that is not allowed (prepared tables only)"
    if _PATHLIKE.search(s):
        return "string literals that look like file paths or URLs are not allowed (prepared tables only)"
    try:
        tree = json.loads(_parser().execute("SELECT json_serialize_sql(?)", [s]).fetchone()[0])
    except Exception as e:
        return f"could not parse the query: {type(e).__name__}"
    if tree.get("error"):
        return f"could not parse the query: {tree.get('error_message', '')[:200]}"
    ctes: set[str] = set()
    refs: list[dict] = []
    _walk(tree, ctes, refs)
    for r in refs:
        if r["type"] == "BASE_TABLE":
            name = (r.get("table_name") or "").lower()
            if (r.get("schema_name") or "") not in ("", "main") or (name not in allowed_tables() and name not in ctes):
                return f"unknown or disallowed table: {name[:80]}"
        elif r["type"] == "TABLE_FUNCTION":
            fn = ((r.get("function") or {}).get("function_name") or "").lower()
            if fn not in _ALLOWED_TABLE_FUNCTIONS:
                return f"table function not allowed: {fn[:80]}"
        elif _FORBIDDEN.fullmatch(r["function_name"] or ""):
            return f"function not allowed: {r['function_name'][:80]}"
    return None


_ALLOWED_TABLE_FUNCTIONS = {"range", "generate_series", "unnest"}
_parser_local = threading.local()


def _parser() -> duckdb.DuckDBPyConnection:
    """A private in-memory connection used only to parse (json_serialize_sql never executes anything)."""
    if not hasattr(_parser_local, "con"):
        _parser_local.con = duckdb.connect(":memory:")
    return _parser_local.con


def _walk(node, ctes: set[str], refs: list[dict]) -> None:
    """Collect table references, table functions and scalar functions from DuckDB's own parse tree."""
    if isinstance(node, dict):
        if node.get("type") in ("BASE_TABLE", "TABLE_FUNCTION"):
            refs.append(node)
        if node.get("class") == "FUNCTION":
            refs.append({"type": "FUNCTION", "function_name": str(node.get("function_name", "")).lower()})
        for m in (node.get("cte_map") or {}).get("map") or []:
            ctes.add(str(m.get("key", "")).lower())
        for v in node.values():
            _walk(v, ctes, refs)
    elif isinstance(node, list):
        for v in node:
            _walk(v, ctes, refs)


def _cell(v):
    if isinstance(v, (int, float, bool, type(None))):
        return v
    v = str(v)
    return v if len(v) <= MAX_CELL_CHARS else v[:MAX_CELL_CHARS] + "…"


def run_sql(sql: str, max_rows: int = 50) -> dict:
    """Read-only SELECT over the prepared tables. Returns at most max_rows rows."""
    s = (sql or "").strip().rstrip(";").strip()
    err = _check_sql(s)
    if err:
        return {"error": err}
    cur = con()
    timer = threading.Timer(SQL_TIMEOUT_S, cur.interrupt)
    timer.start()
    try:
        cur.execute(s)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    except Exception as e:  # surfaced to the model so it can fix its query
        if not timer.is_alive():
            return {"error": f"query stopped after {SQL_TIMEOUT_S:g} s: aggregate or filter more"}
        return {"error": f"{type(e).__name__}: {e}"[:600]}
    finally:
        timer.cancel()
        cur.close()
    truncated = len(rows) > max_rows
    out, size = [], 0
    for r in rows[:max_rows]:
        row = [_cell(v) for v in r]
        size += len(json.dumps(row, default=str))
        if size > MAX_RESULT_CHARS:
            truncated = True
            break
        out.append(row)
    return {"columns": [_cell(c) for c in cols], "rows": out, "row_count": len(out), "truncated": truncated}


SCHEMA_DOC = """Prepared DuckDB tables (read-only):
- zone_features (263 rows, one per NYC taxi zone): zone_id, zone_name, borough, service_zone,
  rent_zori (USD/month, unit-weighted Zillow ZORI Oct 2026; NULL = no rent data), rent_unit_coverage,
  mf_units (residential units in buildings with >=4 units), fha_units_share (units in buildings built >=1991 —
  Fair Housing Act proxy), elevator_units_share (units in building class D* = elevator apartments),
  units_near_ada_share (units within 800 m of an ADA station), ada_stations_800m, ada_stations_full,
  ada_station_names, ada_elevator_uptime (mean 24h availability Apr–Oct 2026), ada_elevator_uptime_worst,
  nearest_ada_m, wav_trips, wav_wait_median_min, wav_wait_p90_min (NULL when < 30 timed WAV trips),
  wav_too_few_rides, wav_trips_per_1000, access_a_ride_trips, ramps, ramp_good_share,
  fha_pct, elevator_pct, ramp_pct, wav_pct (city percentiles 0..1; higher = better).
- zone_stations: zone_id, station_id, complex_id, stop_name, routes, ada (1 full / 2 one direction), ada_notes,
  elevator_uptime, uptime_worst_elevator, n_elevators, dist_m, inside_zone — the ADA stations in reach of each zone.
- stations (496 rows): station_id, complex_id, stop_name, line, routes, lat, lon, ada (0/1/2), ada_notes, zone_id,
  n_elevators, elevator_uptime, uptime_worst_elevator, unscheduled_outages, entrapments.
- elevator_raw: month, equipment_code, complex_id, station_id, station_complex_name, avail_24h, total_outages,
  unscheduled_outages, entrapments (one row per elevator per month, Apr–Oct 2026).
- wav_zone: zone_id, trips_all, wav_trips, access_a_ride_trips, wav_trips_timed, wav_wait_median_min,
  wav_wait_p90_min, all_wait_median_min (HVFHV Sept 2026, completed trips only).
- lots: bbl, zipcode, yearbuilt, numfloors, unitsres, lat, lon, bldgclass, zone_id (PLUTO lots with >=4 units).
- od_fare: pu_zone, do_zone, trips, fare_median, fare_p90 (USD incl. tolls/taxes/surcharges, before tip),
  minutes_median, miles_median — Uber/Lyft trips between zone pairs with >= 20 trips (Sept 2026). score_zones uses it
  for plan_b_ride: the ride from home zone to the work zone, and monthly_cost = 22 workdays × (1 − elevator uptime)
  × 2 rides × fare_median (an estimate, say so).
- city_stats: citywide WAV vs standard pickup-wait medians.
- zone_extra (one row per zone; context, NOT part of the score): area_km2, hpd_elev_buildings / hpd_elev_complaints /
  elev_complaints_per_1000_units (tenant complaints about broken residential elevators, last 12 months),
  lottery_active, lottery_active_units, lottery_active_mobility_units, lottery_active_names (Housing Connect affordable
  lotteries open now; 5% mobility set-aside assumed when blank), bus_stops, bus_routes, bus_stops_per_km2,
  ped_injuries, ped_injuries_per_km2, restrooms_accessible, aps, clinics, hospital_nearest, hospital_nearest_km,
  mobility_disability_pct (CDC model estimate), elev_range_m (hilliness proxy; partial coverage), felonies_per_km2
  (reported felonies 2026 YTD — reflects reporting and policing; only mention if the user asks about crime).
- elevator_outages: complex_id, equipment, station, serving, reason, upcoming, starts, returns, days (MTA live feed snapshot).
- station_upgrades: stations due to become accessible (forecast date may be NULL); zone_upgrades: per zone.
- od_fare_period: like od_fare but per period (am_peak, midday, pm_peak, night, weekend).
- zori: zipcode, City, CountyName, zori.  - ramp_zone: zone_id, ramps, ramps_good.
- zones: zone_id, zone_name, borough, service_zone (+ geometry).  - data_checks: name, value, detail, ok."""


# -------------------------------------------------------------------- geocode
_geo_cache_path = CACHE / "geocode_cache.json"
GEO_CACHE_MAX = 5000
_geo_lock = threading.Lock()
_last_nominatim = 0.0


def _load_geo_cache() -> dict:
    try:
        return json.loads(_geo_cache_path.read_text())
    except Exception:
        return {}


def match_hub(text: str) -> dict | None:
    t = text.lower()
    best = None
    for h in HUBS:
        for a in h["aliases"]:
            if a in t and (best is None or len(a) > best[0]):
                best = (len(a), h)
    return best[1] if best else None


def zone_of(lat: float, lon: float) -> dict | None:
    r = q("""SELECT zone_id, zone_name, borough FROM zones
             WHERE ST_Contains(geom_ft, ST_Transform(ST_Point(?, ?), 'EPSG:4326', 'EPSG:2263', always_xy := true))
             LIMIT 1""", [lon, lat])
    return r[0] if r else None


def geocode(address: str) -> dict:
    """Address or place name → lat/lon + taxi zone. Nominatim first, fixed work hubs as fallback."""
    global _last_nominatim
    address = (address or "").strip()
    if not address:
        return {"error": "empty address", "hubs": [h["name"] for h in HUBS]}
    key = re.sub(r"\s+", " ", address.lower())
    hub = match_hub(address)
    looks_like_street_address = bool(re.search(r"\d", address))
    result = None
    if hub and not looks_like_street_address:
        result = {"lat": hub["lat"], "lon": hub["lon"], "display_name": hub["name"], "source": "built-in work hub"}
    if result is None:
        with _geo_lock:
            cache = _load_geo_cache()
            if key in cache:
                result = dict(cache[key])
            else:
                qs = address if re.search(r"new york|nyc|manhattan|brooklyn|queens|bronx|staten", address, re.I) \
                    else address + ", New York, NY"
                url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
                    "q": qs, "format": "json", "limit": 1,
                    "viewbox": "-74.26,40.92,-73.69,40.49", "bounded": 1})
                try:
                    wait = 1.05 - (time.time() - _last_nominatim)  # Nominatim policy: max 1 request/second
                    if wait > 0:
                        time.sleep(wait)
                    req = urllib.request.Request(url, headers={"User-Agent": "wheelchair-nyc-demo/1.0"})
                    with urllib.request.urlopen(req, timeout=8, context=_SSL) as resp:
                        hits = json.loads(resp.read().decode())
                    _last_nominatim = time.time()
                    if hits:
                        result = {"lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]),
                                  "display_name": hits[0]["display_name"], "source": "Nominatim (OpenStreetMap)"}
                        cache[key] = result
                        while len(cache) > GEO_CACHE_MAX:  # bounded: lookups come from the public
                            cache.pop(next(iter(cache)))
                        _geo_cache_path.parent.mkdir(parents=True, exist_ok=True)
                        _geo_cache_path.write_text(json.dumps(cache, indent=1))
                except Exception as e:
                    result = None
                    geocode_error = f"{type(e).__name__}"
    if result is None and hub:
        result = {"lat": hub["lat"], "lon": hub["lon"], "display_name": hub["name"],
                  "source": "built-in work hub (geocoder unavailable)"}
    if result is None:
        return {"error": f"could not locate '{address}' inside New York City",
                "hint": "try a full street address, or one of the built-in work hubs",
                "hubs": [h["name"] for h in HUBS]}
    z = zone_of(result["lat"], result["lon"])
    if not z:
        return {"error": f"'{address}' resolved outside the five boroughs", "hubs": [h["name"] for h in HUBS]}
    result = {**result, "query": address, **z}
    return result


# -------------------------------------------------------------------- scoring
def _km(a: tuple, b: tuple) -> float:
    """Straight-line distance in km (equirectangular; fine at city scale)."""
    import math
    dy = (a[0] - b[0]) * 111.32
    dx = (a[1] - b[1]) * 111.32 * math.cos(math.radians(40.7))
    return round(math.hypot(dx, dy), 1)


def _routes(s: str) -> set[str]:
    return set((s or "").split())


@lru_cache(maxsize=1)
def _static() -> dict:
    feats = q("SELECT * FROM zone_features ORDER BY zone_id")
    zs = q("SELECT * FROM zone_stations ORDER BY zone_id, dist_m, station_id")
    by_zone: dict[int, list[dict]] = {}
    for s in zs:
        by_zone.setdefault(s["zone_id"], []).append(s)
    ada = q("""SELECT station_id, complex_id, stop_name, routes, ada, ada_notes, lat, lon, zone_id,
                      elevator_uptime, uptime_worst_elevator, n_elevators
               FROM stations WHERE ada IN (1, 2) ORDER BY station_id""")
    pts = {f["properties"]["zone_id"]: (f["properties"]["label_lat"], f["properties"]["label_lon"])
           for f in zones_geojson()["features"]}
    wav_median = q("SELECT median(wav_wait_median_min) AS m FROM zone_features")[0]["m"]
    extra = {r["zone_id"]: r for r in q("""SELECT e.*, u.upgrade_stations, u.upgrade_names, strftime(u.upgrade_earliest, '%b %Y') AS upgrade_earliest
                                           FROM zone_extra e LEFT JOIN zone_upgrades u USING (zone_id)""")}
    out_now: dict[int, list] = {}
    for o in q("""SELECT complex_id, equipment, serving, reason, strftime(starts, '%d %b %Y') AS since, strftime(returns, '%d %b') AS returns, days
                  FROM elevator_outages WHERE NOT upcoming ORDER BY days DESC"""):
        out_now.setdefault(o["complex_id"], []).append(o)
    planned = {r["complex_id"]: r for r in q("""SELECT complex_id, count(*) AS n, strftime(min(starts), '%d %b') AS first, max(days) AS longest_days
                                                FROM elevator_outages WHERE upcoming GROUP BY 1""")}
    backup = {r["complex_id"]: r for r in q("SELECT * FROM elevator_redundancy")}
    feed = q("SELECT strftime(elevator_feed_fetched, '%d %b %H:%M') AS t FROM feed_info")[0]["t"]
    return {"features": feats, "zone_stations": by_zone, "ada": ada, "wav_city_median": wav_median, "pts": pts,
            "extra": extra, "out_now": out_now, "planned": planned, "backup": backup, "feed_time": feed}


EXTRA_KEYS = ("area_km2", "home_lat", "home_lon", "hpd_elev_buildings", "hpd_elev_complaints", "elev_complaints_per_1000_units", "elev_ok_pct",
              "lottery_active", "lottery_active_units", "lottery_active_mobility_units", "lottery_units_3y", "lottery_active_names",
              "bus_stops", "bus_routes", "bus_stops_per_km2", "bus_pct", "ped_injuries", "ped_injuries_per_km2", "ped_safe_pct",
              "restrooms_accessible", "aps", "clinics", "hospital_nearest", "hospital_nearest_km", "mobility_disability_pct",
              "lack_transport_pct", "elev_range_m", "flat_pct", "felonies", "felonies_per_km2", "felony_low_pct",
              "upgrade_stations", "upgrade_names", "upgrade_earliest")


def station_status(complex_id: int | None) -> dict:
    """Live elevator picture for a station complex (snapshot of the MTA feed taken at build time)."""
    st = _static()
    b = st["backup"].get(complex_id)
    return {"out_now": st["out_now"].get(complex_id, []), "planned": st["planned"].get(complex_id),
            "no_backup": bool(b and b["no_backup"] > 0), "as_of": st["feed_time"]}


def zone_alerts(f: dict, hs: dict | None) -> list[dict]:
    """Dated things that are about to change for this zone. kind: warn | good | info."""
    st, ex, out = _static(), _static()["extra"].get(f["zone_id"], {}), []
    if hs:
        s = station_status(hs.get("complex_id"))
        for o in s["out_now"][:2]:
            long = o["days"] >= 7
            out.append({"kind": "warn", "text": f"Elevator {o['equipment']} at {hs['stop_name']} is out right now"
                        + (f" — since {o['since']}, due back {o['returns']} ({o['reason'].lower()})" if long else f", due back {o['returns']} ({o['reason'].lower()})")
                        + f". It serves: {o['serving']}"})
        if s["planned"]:
            p = s["planned"]
            out.append({"kind": "info", "text": f"{p['n']} planned elevator outage{'s' if p['n'] > 1 else ''} at {hs['stop_name']} in the next weeks, first on {p['first']}"
                        + (f" (longest about {p['longest_days']:.0f} days)" if p["longest_days"] >= 1.5 else " (overnight work)")})
        if s["no_backup"] and f["ada_stations_800m"] == 1:
            out.append({"kind": "info", "text": f"{hs['stop_name']} has at least one elevator with no backup, and it is the only accessible station in reach"})
    if ex.get("upgrade_names"):
        out.append({"kind": "good", "text": "Becoming accessible nearby: " + ex["upgrade_names"] + ". MTA forecasts; several earlier ones have slipped"})
    if ex.get("lottery_active"):
        out.append({"kind": "good", "text": f"Open affordable-housing lottery now: {ex['lottery_active_units']:,} units in {ex['lottery_active']} "
                    f"building{'s' if ex['lottery_active'] > 1 else ''}, about {ex['lottery_active_mobility_units']} set aside for mobility disabilities ({ex['lottery_active_names']})"})
    return out



def work_stations(lat: float, lon: float) -> list[dict]:
    return q("""SELECT station_id, complex_id, stop_name, routes, ada, ada_notes, elevator_uptime,
                       uptime_worst_elevator, n_elevators, lat, lon,
                       round(ST_Distance(pt_ft, ST_Transform(ST_Point(?, ?), 'EPSG:4326', 'EPSG:2263', always_xy := true)) * 0.3048)::INT AS dist_m
                FROM stations
                WHERE ada IN (1, 2)
                  AND ST_DWithin(pt_ft, ST_Transform(ST_Point(?, ?), 'EPSG:4326', 'EPSG:2263', always_xy := true), ?)
                ORDER BY dist_m, station_id""", [lon, lat, lon, lat, FT_800M])


def _pct(x: float | None) -> str:
    return "no data" if x is None else f"{round(100 * x)}%"


def _station_rank(s: dict) -> tuple:
    # prefer fully accessible, then within 800 m, then known + higher uptime, then closer
    return (0 if s["ada"] == 1 else 1, 0 if s["dist_m"] <= 800 else 1, 0 if s["elevator_uptime"] is not None else 1,
            -(s["elevator_uptime"] or 0), s["dist_m"], s["station_id"])


def _commute(home: list[dict], work: list[dict], ada_all: list[dict]) -> dict:
    """One-seat accessible ride = a home ADA station and a work ADA station share a daytime route."""
    if not home:
        return {"kind": "no_home_station", "points": 0, "text": "No accessible subway station within reach of home"}
    if not work:
        best = sorted(home, key=_station_rank)[0]
        return {"kind": "no_work_station", "points": 0, "home_station": best,
                "text": "No accessible subway station within 800 m of work — commute relies on bus / accessible ride-hail"}
    pairs = []
    for h in home:
        for w in work:
            shared = _routes(h["routes"]) & _routes(w["routes"])
            if shared:
                pairs.append((_station_rank(h), _station_rank(w), h, w, sorted(shared)))
    if pairs:
        pairs.sort(key=lambda p: (p[0], p[1]))
        _, _, h, w, shared = pairs[0]
        same = h["complex_id"] == w["complex_id"]
        return {"kind": "one_seat", "points": 20, "home_station": h, "work_station": w, "routes": shared,
                "text": ("Work is at the same accessible station complex" if same else
                         f"One-seat accessible ride: {'/'.join(shared)} from {h['stop_name']} to {w['stop_name']}")}
    # one accessible transfer: an ADA complex served by a home route and a work route
    home_routes = set().union(*[_routes(h["routes"]) for h in home])
    work_routes = set().union(*[_routes(w["routes"]) for w in work])
    complexes: dict[int, dict] = {}
    for s in ada_all:
        if s["ada"] != 1:
            continue
        c = complexes.setdefault(s["complex_id"], {"routes": set(), "name": s["stop_name"], "uptime": s["elevator_uptime"]})
        c["routes"] |= _routes(s["routes"])
    cands = []
    for cid, c in complexes.items():
        hr, wr = c["routes"] & home_routes, c["routes"] & work_routes
        if hr and wr:
            cands.append((0 if c["uptime"] is not None else 1, -(c["uptime"] or 0), c["name"], cid, sorted(hr), sorted(wr), c))
    if cands:
        cands.sort(key=lambda t: t[:4])
        _, _, name, cid, hr, wr, c = cands[0]
        h = sorted([h for h in home if _routes(h["routes"]) & set(hr)], key=_station_rank)[0]
        w = sorted([w for w in work if _routes(w["routes"]) & set(wr)], key=_station_rank)[0]
        return {"kind": "transfer", "points": 10, "home_station": h, "work_station": w,
                "transfer": {"name": name, "complex_id": cid, "uptime": c["uptime"],
                             "from_routes": hr, "to_routes": wr},
                "text": f"One accessible transfer: {'/'.join(hr)} from {h['stop_name']}, change at {name} "
                        f"(elevator uptime {_pct(c['uptime'])}) to the {'/'.join(wr)} for {w['stop_name']}"}
    best = sorted(home, key=_station_rank)[0]
    return {"kind": "no_route", "points": 0, "home_station": best,
            "text": "No one-seat or single-transfer accessible subway route found to work"}


def _station_brief(s: dict | None) -> dict | None:
    if not s:
        return None
    return {"station_id": s["station_id"], "name": s["stop_name"], "routes": s["routes"].split(),
            "ada": s["ada"], "ada_notes": s.get("ada_notes"), "dist_m": s.get("dist_m"),
            "uptime": s["elevator_uptime"], "uptime_worst": s["uptime_worst_elevator"],
            "n_elevators": s["n_elevators"], "lat": s.get("lat"), "lon": s.get("lon")}


def _pluses_and_risk(f: dict, c: dict, comp: dict, budget: float) -> tuple[list[str], str, list[str]]:
    pluses: list[tuple[float, str]] = []
    risks: list[tuple[float, str]] = []
    hs = c.get("home_station")
    rent = f["rent_zori"]
    headroom = (budget - rent) / budget

    if c["kind"] == "one_seat":
        pluses.append((1.0, c["text"]))
    elif c["kind"] == "transfer":
        risks.append((0.55, f"Commute needs a transfer at {c['transfer']['name']} — if that elevator is out, the trip breaks"))
    elif c["kind"] == "no_route":
        risks.append((0.9, "No accessible subway route to work with at most one transfer"))
    elif c["kind"] == "no_work_station":
        risks.append((0.85, "No accessible station near work — the daily commute depends on bus or accessible ride-hail"))

    if hs:
        u = hs["elevator_uptime"]
        if u is None:
            risks.append((0.3, f"No elevator data for {hs['stop_name']} — check how the station is made accessible"))
        elif u >= 0.97:
            pluses.append((0.6 + (u - 0.97) * 10, f"Elevators at {hs['stop_name']} were available {100*u:.1f}% of the time (Apr–Oct 2026)"))
        elif u < 0.92:
            risks.append((0.6 + (0.92 - u) * 3, f"Elevators at {hs['stop_name']} were available only {100*u:.0f}% of the time (Apr–Oct 2026)"))
        w = hs["uptime_worst_elevator"]
        if u is not None and w is not None and w < 0.85 and u >= 0.92:
            risks.append((0.5 + (0.85 - w) * 0.5, f"One elevator at {hs['stop_name']} was available only {100*w:.0f}% of the time — one outage can cut off a platform"))
        if hs["ada"] == 2:
            risks.append((0.7, f"{hs['stop_name']} is accessible in one direction only ({hs.get('ada_notes') or 'see MTA notes'})"))

    n = f["ada_stations_800m"]
    if n >= 3:
        pluses.append((0.5 + min(n, 8) / 40, f"{n} accessible stations in reach — a backup when one elevator is out"))
    elif n == 1:
        risks.append((0.45, "Only one accessible station in reach — no backup when its elevator is out"))

    una = f["units_near_ada_share"]
    if una is not None:
        if una >= 0.8:
            pluses.append((0.45 + una / 10, f"{_pct(una)} of apartments are within 800 m of an accessible station"))
        elif una < 0.4:
            risks.append((0.5 + (0.4 - una), f"Only {_pct(una)} of apartments are within 800 m of an accessible station — the address matters"))

    if f["elevator_pct"] is not None:
        if f["elevator_pct"] >= 0.65:
            pluses.append((f["elevator_pct"] * 0.8, f"{_pct(f['elevator_units_share'])} of apartments are in elevator buildings"))
        elif f["elevator_pct"] < 0.3:
            risks.append((0.5 + (0.3 - f["elevator_pct"]) * 0.6, f"Mostly walk-up buildings — only {_pct(f['elevator_units_share'])} of apartments are in elevator buildings"))
    if f["fha_pct"] is not None and f["fha_pct"] >= 0.65:
        pluses.append((f["fha_pct"] * 0.75, f"{_pct(f['fha_units_share'])} of apartments are in post-1991 buildings (built under accessibility rules — a proxy)"))
    if f["wav_pct"] is not None:
        if f["wav_pct"] >= 0.65:
            pluses.append((f["wav_pct"] * 0.6, f"Accessible Uber/Lyft arrives in about {f['wav_wait_median_min']:.1f} min (median)"))
        elif f["wav_pct"] < 0.25:
            risks.append((0.35 + (0.25 - f["wav_pct"]), f"Slow plan B: accessible Uber/Lyft median wait is {f['wav_wait_median_min']:.1f} min, 1 in 10 waits over {f['wav_wait_p90_min']:.0f} min"))
    else:
        risks.append((0.3, "Too few accessible ride-hail trips here to measure a wait"))
    if f["ramp_pct"] is not None:
        if f["ramp_pct"] >= 0.65:
            pluses.append((f["ramp_pct"] * 0.55, f"{_pct(f['ramp_good_share'])} of curb ramps have a warning surface in good condition"))
        elif f["ramp_pct"] < 0.2:
            risks.append((0.35 + (0.2 - f["ramp_pct"]), f"Curb ramps: only {_pct(f['ramp_good_share'])} have a warning surface in good condition"))
    if headroom >= 0.1:
        pluses.append((0.4 + min(headroom, 0.4), f"Typical rent ${rent:,} is ${budget - rent:,.0f} under budget"))
    elif headroom < 0.04:
        risks.append((0.5, f"Typical rent ${rent:,} is right at the budget — accessible units often cost more"))
    ex = _static()["extra"].get(f["zone_id"], {})
    if hs:
        long_out = [o for o in station_status(hs.get("complex_id"))["out_now"] if o["days"] >= 7]
        if long_out:
            o = long_out[0]
            risks.append((0.95, f"An elevator at {hs['stop_name']} has been out since {o['since']} (due back {o['returns']}) — check it is not the one you need"))
    if ex.get("lottery_active"):
        pluses.append((0.56, f"Open affordable-housing lottery now: {ex['lottery_active_units']:,} units, about {ex['lottery_active_mobility_units']} set aside for mobility disabilities"))
    if ex.get("bus_pct") is not None and ex["bus_pct"] >= 0.75 and ex["bus_stops"]:
        pluses.append((0.42, f"{ex['bus_stops']} bus stops on {ex['bus_routes']} routes — every NYC bus has a ramp or lift"))
    if ex.get("elev_ok_pct") is not None and ex["elev_ok_pct"] < 0.15:
        risks.append((0.44, f"Broken-elevator complaints in {ex['hpd_elev_buildings']} residential buildings here in the last 12 months — among the highest rates in the city"))
    if f["rent_unit_coverage"] is not None and f["rent_unit_coverage"] < 0.5:
        risks.append((0.4, f"Rent figure covers only {_pct(f['rent_unit_coverage'])} of this zone's apartments"))

    pluses.sort(key=lambda t: (-t[0], t[1]))
    risks.sort(key=lambda t: (-t[0], t[1]))
    if not risks:
        risks.append((0, "Typical rent is not an accessible-unit rent — availability of an adapted apartment is not in the data"))
    return [p[1] for p in pluses[:3]], risks[0][1], [r[1] for r in risks[1:3]]


def score(budget: float, work_lat: float, work_lon: float, work_label: str | None = None) -> dict:
    """Rank all taxi zones for a wheelchair user with this budget and workplace (research/02)."""
    budget = float(budget)
    st = _static()
    wz = zone_of(work_lat, work_lon)
    wst = work_stations(work_lat, work_lon)
    work_has_station = bool(wst)
    od = {r["pu_zone"]: r for r in q("SELECT * FROM od_fare WHERE do_zone = ?", [wz["zone_id"]])} if wz else {}

    ranked, deserts, zone_status = [], [], {}
    counts = {"zones": len(st["features"]), "no_rent_data": 0, "little_housing": 0, "over_budget": 0,
              "affordable": 0, "affordable_no_station": 0, "qualifying": 0}
    for f in st["features"]:
        zid = f["zone_id"]
        if f["rent_zori"] is None:
            counts["no_rent_data"] += 1
            zone_status[zid] = {"status": "no_rent_data"}
            continue
        if f["mf_units"] < MIN_UNITS:
            counts["little_housing"] += 1
            zone_status[zid] = {"status": "little_housing", "rent": f["rent_zori"]}
            continue
        if f["rent_zori"] > budget:
            counts["over_budget"] += 1
            zone_status[zid] = {"status": "over_budget", "rent": f["rent_zori"]}
            continue
        counts["affordable"] += 1
        home = st["zone_stations"].get(zid, [])
        if not home:
            counts["affordable_no_station"] += 1
            slow = f["wav_wait_median_min"] is not None and f["wav_wait_median_min"] >= st["wav_city_median"]
            unmeasured = f["wav_wait_median_min"] is None
            is_desert = slow or unmeasured
            zone_status[zid] = {"status": "desert" if is_desert else "no_station", "rent": f["rent_zori"]}
            if is_desert:
                deserts.append({
                    "zone_id": zid, "zone_name": f["zone_name"], "borough": f["borough"], "rent": f["rent_zori"],
                    "nearest_ada_m": f["nearest_ada_m"], "units_near_ada_share": f["units_near_ada_share"],
                    "mf_units": f["mf_units"],
                    "wav_wait_median_min": f["wav_wait_median_min"], "wav_wait_p90_min": f["wav_wait_p90_min"],
                    "wav_status": "too few rides to measure" if unmeasured else "slow",
                    "elevator_units_share": f["elevator_units_share"]})
            if work_has_station:
                continue  # hard filter 2: needs an ADA station at home
        c = _commute(home, wst, st["ada"])
        hs = c.get("home_station")
        u = hs["elevator_uptime"] if hs else None
        headroom = (budget - f["rent_zori"]) / budget
        comp = {
            "housing": round(15 * (f["fha_pct"] or 0) + 15 * (f["elevator_pct"] or 0), 1),
            "transit": round(c["points"] + (10 * max(0.0, min(1.0, (u - 0.80) / 0.20)) if u is not None else 0), 1),
            "street": round(15 * (f["ramp_pct"] or 0), 1),
            "plan_b": round(15 * (f["wav_pct"] or 0), 1),
            "rent": round(10 * max(0.0, min(1.0, headroom / 0.40)), 1),
        }
        total = round(sum(comp.values()), 1)
        pluses, risk, other_risks = _pluses_and_risk(f, c, comp, budget)
        ex = st["extra"].get(zid, {})
        alerts = zone_alerts(f, hs)
        ride = od.get(zid)
        plan_b = None
        if ride:
            # days a month the home elevator is expected to be out, if it fails as often as Apr–Oct 2026
            out_days = round(WORKDAYS * (1 - u), 1) if u is not None else None
            plan_b = {"fare": ride["fare_median"], "fare_p90": ride["fare_p90"], "minutes": ride["minutes_median"],
                      "trips": ride["trips"], "outage_days_per_month": out_days,
                      "monthly_cost": round(out_days * 2 * ride["fare_median"]) if out_days is not None else None}
        missing = [k for k, v in (("housing", f["fha_pct"]), ("street", f["ramp_pct"]), ("plan_b", f["wav_pct"])) if v is None]
        if hs and u is None:
            missing.append("elevator uptime")
        ranked.append({
            "zone_id": zid, "zone_name": f["zone_name"], "borough": f["borough"],
            "score": total, "components": comp, "missing_data": missing,
            "rent": f["rent_zori"], "rent_headroom": round(budget - f["rent_zori"]),
            "rent_unit_coverage": f["rent_unit_coverage"],
            "home_station": _station_brief(hs), "ada_stations": f["ada_stations_800m"],
            "ada_station_names": f["ada_station_names"],
            "commute": {"kind": c["kind"], "text": c["text"], "routes": c.get("routes"),
                        "work_station": _station_brief(c.get("work_station")),
                        "transfer": c.get("transfer")},
            "one_seat": c["kind"] == "one_seat",
            "wav_wait_median_min": f["wav_wait_median_min"], "wav_wait_p90_min": f["wav_wait_p90_min"],
            "wav_trips": f["wav_trips"],
            "fha_units_share": f["fha_units_share"], "elevator_units_share": f["elevator_units_share"],
            "units_near_ada_share": f["units_near_ada_share"], "ramp_good_share": f["ramp_good_share"],
            "mf_units": f["mf_units"],
            "dist_work_km": _km(st["pts"][zid], (work_lat, work_lon)),
            "plan_b_ride": plan_b,
            "extra": {k: ex.get(k) for k in EXTRA_KEYS}, "alerts": alerts,
            "pluses": pluses, "risk": risk, "other_risks": other_risks,
        })
    ranked.sort(key=lambda r: (-r["score"], r["rent"], r["zone_id"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
        zone_status[r["zone_id"]] = {"status": "qualifies", "score": r["score"], "rank": i, "rent": r["rent"]}
    counts["qualifying"] = len(ranked)
    deserts.sort(key=lambda d: (-(d["wav_wait_median_min"] or 0), d["rent"], d["zone_id"]))
    counts["deserts"] = len(deserts)
    counts["desert_units"] = sum(d["mf_units"] for d in deserts)

    notes = []
    if not work_has_station:
        notes.append("No accessible subway station within 800 m of the workplace. The home-station filter is relaxed "
                     "and zones are ranked without subway-commute points: the commute would rely on bus or accessible ride-hail.")
    return {
        "params": {"budget": budget, "work_lat": work_lat, "work_lon": work_lon, "work_label": work_label},
        "work": {"label": work_label, "lat": work_lat, "lon": work_lon, "zone": wz,
                 "stations": [_station_brief(s) for s in wst]},
        "weights": WEIGHTS, "counts": counts, "notes": notes,
        "wav_city_median_min": st["wav_city_median"],
        "top3": ranked[:3], "ranked": ranked, "deserts": deserts, "zone_status": zone_status,
        "method_sql": METHOD_SQL.strip().replace("$BUDGET", f"{budget:.0f}"),
    }


METHOD_SQL = """
-- 1. hard filter: typical rent within budget (zones without rent data are shown grey, not ranked)
--    and zones with fewer than 1,000 multifamily units (parks, airports, industrial areas) are skipped
SELECT zone_id, zone_name, rent_zori FROM zone_features WHERE rent_zori <= $BUDGET AND mf_units >= 1000;

-- 2. hard filter: at least one ADA station in reach of home (inside the zone or <= 800 m from its residential centre)
SELECT z.zone_id, s.stop_name, s.routes, s.ada, s.elevator_uptime, s.dist_m
FROM zone_features z JOIN zone_stations s USING (zone_id) WHERE z.rent_zori <= $BUDGET;

-- 3. ADA stations within 800 m of the workplace (EPSG:4326 point → EPSG:2263 feet)
SELECT stop_name, routes, elevator_uptime FROM stations
WHERE ada IN (1, 2) AND ST_DWithin(pt_ft, ST_Transform(ST_Point(:work_lon, :work_lat), 'EPSG:4326', 'EPSG:2263', always_xy := true), 800 / 0.3048);

-- 4. commute check (in Python, on the rows above): one-seat ride = a home station and a work station share a
--    daytime route; otherwise look for one fully accessible transfer complex serving a home route and a work route.

-- 5. score = 15*fha_pct + 15*elevator_pct            -- housing (30)
--          + commute (20 one-seat | 10 transfer) + 10*clamp((uptime - 0.80) / 0.20)   -- transit (30)
--          + 15*ramp_pct                              -- street (15)
--          + 15*wav_pct                               -- plan B (15)
--          + 10*clamp(((budget - rent) / budget) / 0.40)                              -- rent headroom (10)

-- 6. deserts: affordable, no ADA station in reach, and accessible ride-hail slow (>= city median) or unmeasurable
SELECT zone_name, rent_zori, wav_wait_median_min FROM zone_features
WHERE rent_zori <= $BUDGET AND mf_units >= 1000 AND ada_stations_800m = 0
  AND (wav_wait_median_min IS NULL OR wav_wait_median_min >= (SELECT median(wav_wait_median_min) FROM zone_features));
"""


def compact_for_model(res: dict, n: int = 8) -> dict:
    """A small view of the score result for the model's context (full result goes to the UI)."""
    def slim(r):
        return {k: r[k] for k in ("rank", "zone_id", "zone_name", "borough", "score", "components", "rent",
                                  "home_station", "commute", "wav_wait_median_min", "wav_wait_p90_min",
                                  "fha_units_share", "elevator_units_share", "units_near_ada_share",
                                  "ramp_good_share", "dist_work_km", "plan_b_ride", "alerts", "pluses", "risk", "other_risks", "missing_data")}
    return {"params": res["params"], "work": res["work"], "counts": res["counts"], "notes": res["notes"],
            "weights": {k: {"max": v["max"], "how": v["how"]} for k, v in res["weights"].items()},
            "top": [slim(r) for r in res["ranked"][:n]],
            "deserts_top": res["deserts"][:6],
            "note": "The UI already shows the full ranked table, the map and the deserts view from this result."}


# ---------------------------------------------------------------------- map
@lru_cache(maxsize=1)
def zones_geojson() -> dict:
    return json.loads((DATA / "zones.geojson").read_text())


def stations_for_map() -> list[dict]:
    return q("""SELECT station_id, stop_name AS name, routes, ada, ada_notes, lat, lon, zone_id,
                       elevator_uptime AS uptime, uptime_worst_elevator AS uptime_worst, n_elevators
                FROM stations WHERE ada IN (1, 2) ORDER BY station_id""")


_png_lock = threading.Lock()


def render_map_png(res: dict, view: str = "score") -> bytes:
    """Static PNG of the result — this is the image the model looks at for its self-check."""
    import math

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import colormaps
    from matplotlib.collections import PatchCollection
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, PathPatch
    from matplotlib.path import Path as MPath

    gj = zones_geojson()
    status = res["zone_status"]
    cmap = colormaps["viridis"]
    k = math.cos(math.radians(40.7))

    def polys(geom):
        return geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]

    def patch(geom):
        verts, codes = [], []
        for poly in polys(geom):
            for ring in poly:
                verts += [(x * k, y) for x, y in ring]
                codes += [MPath.MOVETO] + [MPath.LINETO] * (len(ring) - 2) + [MPath.CLOSEPOLY]
        return PathPatch(MPath(verts, codes))

    top = res["top3"]
    top_ids = {r["zone_id"]: r["rank"] for r in top}
    with _png_lock:
        fig, axes = plt.subplots(1, 2, figsize=(15, 8.4), dpi=100, gridspec_kw={"width_ratios": [1.15, 1]})
        fig.patch.set_facecolor("#fbf7ee")
        label_pts = {}
        for ax_i, ax in enumerate(axes):
            ax.set_facecolor("#dfe8ee")
            for feat in gj["features"]:
                p = feat["properties"]
                zid = p["zone_id"]
                s = status.get(zid, {"status": "no_rent_data"})
                pp = patch(feat["geometry"])
                if view == "deserts":
                    if s["status"] == "desert":
                        pp.set(facecolor="#b3261e", edgecolor="#fff", linewidth=0.5, hatch="///")
                    elif s["status"] == "qualifies":
                        pp.set(facecolor="#9ad1c2", edgecolor="#fff", linewidth=0.4)
                    else:
                        pp.set(facecolor="#ece7dc", edgecolor="#fff", linewidth=0.4)
                elif s["status"] == "qualifies":
                    pp.set(facecolor=cmap(max(0.0, min(1.0, (s["score"] - 30) / 60))), edgecolor="#fff", linewidth=0.4)
                elif s["status"] in ("no_rent_data", "little_housing"):
                    pp.set(facecolor="#c9c4b8", edgecolor="#fff", linewidth=0.4, hatch="...")
                elif s["status"] in ("desert", "no_station"):
                    pp.set(facecolor="#f3ede0", edgecolor="#8a8375", linewidth=0.4, hatch="xx")
                else:
                    pp.set(facecolor="#f3ede0", edgecolor="#fff", linewidth=0.4)
                ax.add_patch(pp)
                label_pts[zid] = (p["label_lon"] * k, p["label_lat"])
            for feat in gj["features"]:
                zid = feat["properties"]["zone_id"]
                if zid in top_ids:
                    o = patch(feat["geometry"]); o.set(facecolor="none", edgecolor="#fff", linewidth=5.5); ax.add_patch(o)
                    o = patch(feat["geometry"]); o.set(facecolor="none", edgecolor="#000", linewidth=2.6); ax.add_patch(o)
            for stn in stations_for_map():
                u = stn["uptime"]
                m, c = ("o", "#0b6e4f") if (u is not None and u >= 0.95) else ("^", "#e69f00") if (u is not None and u >= 0.85) \
                    else ("s", "#b3261e") if u is not None else ("D", "#666")
                ax.plot(stn["lon"] * k, stn["lat"], marker=m, color=c, markersize=4.5 if ax_i == 0 else 8,
                        markeredgecolor="#fff", markeredgewidth=0.6, linestyle="none", zorder=4)
            ax.plot(res["work"]["lon"] * k, res["work"]["lat"], marker="*", color="#d81b60", markersize=20,
                    markeredgecolor="#000", markeredgewidth=1.2, zorder=6)
            for r in top:
                x, y = label_pts[r["zone_id"]]
                ax.annotate(f"{r['rank']}  {r['zone_name']} · {r['score']:.0f}" if ax_i == 1 else str(r["rank"]),
                            (x, y), ha="center", va="center", fontsize=11 if ax_i == 1 else 12, fontweight="bold",
                            color="#000", zorder=7,
                            bbox=dict(boxstyle="round,pad=0.3", fc="#ffe45c", ec="#000", lw=1.4))
            ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
            for sp in ax.spines.values():
                sp.set_color("#14110f")
        axes[0].set_xlim(-74.27 * k, -73.69 * k); axes[0].set_ylim(40.49, 40.92)
        xs = [label_pts[r["zone_id"]][0] for r in top] + [res["work"]["lon"] * k]
        ys = [label_pts[r["zone_id"]][1] for r in top] + [res["work"]["lat"]]
        if xs:
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            half = max(max(xs) - min(xs), max(ys) - min(ys), 0.03) / 2 + 0.022
            axes[1].set_xlim(cx - half, cx + half); axes[1].set_ylim(cy - half, cy + half)
        b = res["params"]["budget"]
        wl = res["work"]["label"] or "workplace"
        axes[0].set_title(("Accessibility deserts" if view == "deserts" else "Accessibility score by taxi zone")
                          + f" — budget ${b:,.0f}, work: {wl}", fontsize=12, loc="left", fontweight="bold")
        axes[1].set_title("Zoom: top-3 zones (numbered, outlined) and workplace (star)", fontsize=12, loc="left", fontweight="bold")
        handles = [
            Patch(fc=cmap(0.9), ec="#fff", label="qualifies — score near 90 (yellow)"),
            Patch(fc=cmap(0.5), ec="#fff", label="qualifies — score near 60 (teal)"),
            Patch(fc=cmap(0.1), ec="#fff", label="qualifies — score near 30 (purple)"),
            Patch(fc="#f3ede0", ec="#8a8375", hatch="xx", label="affordable, no accessible station"),
            Patch(fc="#f3ede0", ec="#aaa", label="over budget"),
            Patch(fc="#c9c4b8", ec="#fff", hatch="...", label="no rent data / little housing"),
            Line2D([], [], marker="o", color="#0b6e4f", ls="none", label="station, elevators up ≥95%"),
            Line2D([], [], marker="^", color="#e69f00", ls="none", label="station, 85–95%"),
            Line2D([], [], marker="s", color="#b3261e", ls="none", label="station, <85%"),
            Line2D([], [], marker="D", color="#666", ls="none", label="station, no elevator data"),
            Line2D([], [], marker="*", color="#d81b60", mec="#000", ms=14, ls="none", label="workplace"),
        ]
        if view == "deserts":
            handles = [Patch(fc="#b3261e", ec="#fff", hatch="///", label="desert: affordable, no station, slow ride-hail"),
                       Patch(fc="#9ad1c2", ec="#fff", label="qualifies")] + handles[4:]
        axes[0].legend(handles=handles, loc="upper left", fontsize=8.5, framealpha=0.95, edgecolor="#14110f")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
    return buf.getvalue()


# ------------------------------------------------------- offline question parser
def parse_question(text: str) -> dict:
    """Rule-based fallback used only when no Claude API credential is available."""
    t = text.replace(",", "")
    budget = None
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(k)?", t, re.I) or re.search(r"(\d+(?:\.\d+)?)\s*(k)?\s*(?:usd|dollars|/\s*mo|a month|per month)", t, re.I) \
        or re.search(r"budget[^\d]{0,20}(\d+(?:\.\d+)?)\s*(k)?", t, re.I)
    if m:
        budget = float(m.group(1)) * (1000 if m.group(2) else 1)
        if budget < 100:
            budget *= 1000
    work = None
    m = re.search(r"\b(?:work(?:ing|s)?|job|office|commute)\b[^.;\n]*?\b(?:at|in|near|to|on|is)\s+([^.;\n]+)", text, re.I)
    if m:
        work = re.sub(r"\s*(?:and|,)?\s*(?:my )?budget.*$", "", m.group(1), flags=re.I).strip(" ,.")
    if not work:
        hub = match_hub(text)
        work = hub["name"] if hub else None
    return {"budget": budget, "work": work}
