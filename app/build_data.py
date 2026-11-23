"""Step 1 — data layer.

Builds data/prepared.duckdb (tables: zones, stations, zone_stations, zone_features, data_checks),
data/zone_features.parquet and data/zones.geojson. Prints row counts, join match rates and samples.

Run:  .venv/bin/python app/build_data.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "prepared.duckdb"
FT_800M = 800 / 0.3048  # EPSG:2263 is in US survey feet; 800 m ≈ 2,624.7 ft
MIN_WAV_TRIPS = 30

checks: list[dict] = []


def check(name: str, value, detail: str = "", ok: bool = True) -> None:
    checks.append({"name": name, "value": value, "detail": detail, "ok": bool(ok)})
    flag = "ok " if ok else "WARN"
    print(f"  [{flag}] {name}: {value}  {detail}")


def main() -> None:
    t0 = time.time()
    os.chdir(ROOT)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    con.sql("INSTALL spatial; LOAD spatial;")
    con.sql(f"SET VARIABLE ft800 = {FT_800M}")

    # ---------------------------------------------------------------- zones
    con.sql("""
        CREATE TABLE zones AS
        SELECT z.LocationID::INT AS zone_id, z.zone AS zone_name, z.borough,
               l.service_zone,
               ST_MakeValid(z.geom) AS geom_ft,                          -- EPSG:2263 (feet)
               ST_Centroid(z.geom) AS centroid_ft
        FROM ST_Read('/vsizip/data/taxi_zones.zip/taxi_zones/taxi_zones.shp') z
        LEFT JOIN read_csv('data/taxi_zone_lookup.csv') l ON l.LocationID = z.LocationID
    """)
    n_zones = con.sql("SELECT count(*) FROM zones").fetchall()[0][0]
    check("taxi zones loaded", n_zones, "expected 263", n_zones == 263)

    # ------------------------------------------------------------- stations
    con.sql("""
        CREATE TABLE stations_raw AS
        SELECT station_id::INT AS station_id, complex_id::INT AS complex_id, stop_name,
               line, borough AS boro_code, daytime_routes,
               gtfs_latitude::DOUBLE AS lat, gtfs_longitude::DOUBLE AS lon,
               ada::INT AS ada,
               CASE WHEN ada_notes IN ('NaN', '') THEN NULL ELSE ada_notes END AS ada_notes,
               ST_Transform(ST_Point(gtfs_longitude::DOUBLE, gtfs_latitude::DOUBLE),
                            'EPSG:4326', 'EPSG:2263', always_xy := true) AS pt_ft
        FROM read_csv('data/subway_stations.csv', all_varchar = true)
    """)
    r = con.sql("SELECT count(*), count(*) FILTER (ada=1), count(*) FILTER (ada=2), count(*) FILTER (ada=0) FROM stations_raw").fetchall()[0]
    check("subway station rows", r[0], f"ada full={r[1]}, partial={r[2]}, none={r[3]}")

    # ------------------------------------------------- elevator availability
    con.sql("""
        CREATE TABLE elevator_raw AS
        SELECT month::TIMESTAMP AS month, equipment_code,
               station_complex_mrn::INT AS complex_id, station_mrn::INT AS station_id,
               station_complex_name, _24_hour_availability::DOUBLE AS avail_24h,
               total_outages::INT AS total_outages, unscheduled_outages::INT AS unscheduled_outages,
               entrapments::INT AS entrapments
        FROM read_csv('data/elevator_availability.csv', all_varchar = true)
        WHERE equipment_type = 'Elevator'
    """)
    r = con.sql("SELECT count(*), count(DISTINCT equipment_code), count(DISTINCT complex_id), min(month)::DATE, max(month)::DATE FROM elevator_raw").fetchall()[0]
    check("elevator-month rows", r[0], f"{r[1]} elevators in {r[2]} complexes, {r[3]} → {r[4]}")

    con.sql("""
        CREATE TABLE elevator_complex AS
        WITH dev AS (
            SELECT complex_id, equipment_code, avg(avail_24h) AS dev_avail,
                   sum(unscheduled_outages) AS unsched, sum(entrapments) AS entrap
            FROM elevator_raw GROUP BY ALL)
        SELECT complex_id, count(*) AS n_elevators,
               avg(dev_avail) AS uptime_mean,          -- mean 24h availability, Apr–Oct 2026
               min(dev_avail) AS uptime_worst_elevator, -- the single worst elevator in the complex
               sum(unsched) AS unscheduled_outages, sum(entrap) AS entrapments
        FROM dev GROUP BY complex_id
    """)
    r = con.sql("""SELECT count(*), count(*) FILTER (complex_id IN (SELECT complex_id FROM stations_raw))
                   FROM elevator_complex""").fetchall()[0]
    check("JOIN elevator complex → station complex", f"{r[1]}/{r[0]}",
          f"{100*r[1]/r[0]:.1f}% matched on station_complex_mrn = complex_id", r[1] / r[0] >= 0.7)

    con.sql("""
        CREATE TABLE stations AS
        SELECT s.station_id, s.complex_id, s.stop_name, s.line, s.daytime_routes AS routes,
               s.lat, s.lon, s.ada, s.ada_notes,
               (SELECT z.zone_id FROM zones z WHERE ST_Contains(z.geom_ft, s.pt_ft) LIMIT 1) AS zone_id,
               e.n_elevators, e.uptime_mean AS elevator_uptime,
               e.uptime_worst_elevator, e.unscheduled_outages, e.entrapments,
               s.pt_ft
        FROM stations_raw s LEFT JOIN elevator_complex e USING (complex_id)
    """)
    r = con.sql("""SELECT count(*) FILTER (ada IN (1,2)),
                          count(*) FILTER (ada IN (1,2) AND elevator_uptime IS NOT NULL),
                          count(*) FILTER (zone_id IS NULL) FROM stations""").fetchall()[0]
    check("ADA stations with elevator data", f"{r[1]}/{r[0]}",
          f"{100*r[1]/r[0]:.1f}% — the rest are shown as 'no elevator data' (ramps / at-grade / not in feed)")
    check("stations not inside any taxi zone", r[2], "", r[2] <= 5)

    # ------------------------------------------------------------ PLUTO lots
    con.sql("""
        CREATE TABLE lots_raw AS
        SELECT bbl, zipcode, yearbuilt::INT AS yearbuilt, numfloors::DOUBLE AS numfloors,
               unitsres::INT AS unitsres, latitude::DOUBLE AS lat, longitude::DOUBLE AS lon, bldgclass
        FROM read_csv('data/pluto_multifamily.csv', all_varchar = true)
    """)
    con.sql("""
        CREATE TABLE lots AS
        SELECT l.*, z.zone_id,
               ST_Transform(ST_Point(l.lon, l.lat), 'EPSG:4326', 'EPSG:2263', always_xy := true) AS pt_ft
        FROM lots_raw l
        LEFT JOIN zones z
          ON l.lat IS NOT NULL
         AND ST_Contains(z.geom_ft, ST_Transform(ST_Point(l.lon, l.lat), 'EPSG:4326', 'EPSG:2263', always_xy := true))
    """)
    r = con.sql("SELECT count(*), count(zone_id), sum(unitsres), sum(unitsres) FILTER (zone_id IS NOT NULL), count(DISTINCT bbl) FROM lots").fetchall()[0]
    check("PLUTO multifamily lots (>=4 units)", r[0], f"{r[2]:,} residential units")
    check("JOIN lots → zones (point in polygon)", f"{r[1]}/{r[0]}",
          f"{100*r[1]/r[0]:.2f}% of lots, {100*r[3]/r[2]:.2f}% of units", r[1] / r[0] >= 0.97)
    check("lots duplicated by the join", r[0] - r[4], "0 = each lot fell in exactly one zone", r[0] == r[4])

    # units within 800 m of an ADA station
    con.sql("""
        CREATE TABLE lot_ada AS
        SELECT l.bbl, min(ST_Distance(l.pt_ft, s.pt_ft)) * 0.3048 AS nearest_ada_m
        FROM lots l JOIN stations s
          ON s.ada IN (1,2) AND ST_DWithin(l.pt_ft, s.pt_ft, getvariable('ft800'))
        WHERE l.zone_id IS NOT NULL
        GROUP BY l.bbl
    """)

    # ------------------------------------------------------------------ rent
    zori_cols = [c[0] for c in con.sql("DESCRIBE SELECT * FROM read_csv('data/zori_zip.csv')").fetchall()]
    month_cols = [c for c in zori_cols if c[:2] == "20"]
    latest = month_cols[-1]
    recent = month_cols[-3:]  # tolerate a ZIP missing the very last month
    coalesce = ", ".join(f'"{c}"' for c in reversed(recent))
    con.sql(f"""
        CREATE TABLE zori AS
        SELECT lpad(RegionName::VARCHAR, 5, '0') AS zipcode, City, CountyName,
               coalesce({coalesce})::DOUBLE AS zori
        FROM read_csv('data/zori_zip.csv')
        WHERE State = 'NY'
    """)
    con.sql("DELETE FROM zori WHERE zori IS NULL")
    r = con.sql("""SELECT count(DISTINCT zipcode), count(DISTINCT zipcode) FILTER (zipcode IN (SELECT zipcode FROM zori)),
                          sum(unitsres), sum(unitsres) FILTER (zipcode IN (SELECT zipcode FROM zori))
                   FROM lots WHERE zone_id IS NOT NULL""").fetchall()[0]
    check("ZORI latest month used", latest, f"(falls back to the previous 2 months if a ZIP is blank)")
    check("JOIN ZIP → rent (ZORI)", f"{r[1]}/{r[0]} ZIPs",
          f"{100*r[1]/r[0]:.1f}% of PLUTO ZIPs, covering {100*r[3]/r[2]:.1f}% of multifamily units")

    # ---------------------------------------------------------------- ramps
    ramp_files = sorted(glob.glob("data/ramps_*.csv")) or ["data/ramps.csv"]
    con.sql(f"""
        CREATE TABLE ramps AS
        SELECT rampid, dws_conditions,
               ST_Transform(ST_GeomFromText(the_geom), 'EPSG:4326', 'EPSG:2263', always_xy := true) AS pt_ft
        FROM read_csv({ramp_files!r}, all_varchar = true, union_by_name = true)
        WHERE the_geom IS NOT NULL
    """)
    con.sql("""
        CREATE TABLE ramp_zone AS
        SELECT z.zone_id, count(*) AS ramps,
               count(*) FILTER (r.dws_conditions = 'Good Condition') AS ramps_good
        FROM ramps r JOIN zones z ON ST_Contains(z.geom_ft, r.pt_ft)
        GROUP BY z.zone_id
    """)
    r = con.sql("SELECT (SELECT count(*) FROM ramps), (SELECT count(DISTINCT rampid) FROM ramps), sum(ramps), sum(ramps_good) FROM ramp_zone").fetchall()[0]
    check("curb ramps loaded", r[0], f"{r[1]} distinct ids; source reports 217,679 (re-fetched: first download was truncated)", r[0] > 210000)
    check("JOIN ramps → zones", f"{r[2]}/{r[0]}", f"{100*r[2]/r[0]:.1f}% matched; {100*r[3]/r[2]:.1f}% have a detectable warning surface in good condition")

    # ------------------------------------------------------------- WAV rides
    con.sql("""
        CREATE TABLE wav_zone AS
        SELECT PULocationID::INT AS zone_id,
               count(*) AS trips_all,
               count(*) FILTER (wav_request_flag = 'Y') AS wav_trips,
               count(*) FILTER (access_a_ride_flag = 'Y') AS access_a_ride_trips,
               count(*) FILTER (wav_request_flag = 'Y' AND w BETWEEN 0 AND 3600) AS wav_trips_timed,
               median(w) FILTER (wav_request_flag = 'Y' AND w BETWEEN 0 AND 3600) / 60.0 AS wav_wait_median_min,
               quantile_cont(w, 0.9) FILTER (wav_request_flag = 'Y' AND w BETWEEN 0 AND 3600) / 60.0 AS wav_wait_p90_min,
               median(w) FILTER (w BETWEEN 0 AND 3600) / 60.0 AS all_wait_median_min
        FROM (SELECT PULocationID, wav_request_flag, access_a_ride_flag,
                     date_diff('second', request_datetime, on_scene_datetime) AS w
              FROM 'data/fhvhv_2026-09.parquet')
        GROUP BY 1
    """)
    r = con.sql("""SELECT sum(trips_all), sum(wav_trips), sum(wav_trips_timed),
                          count(*) FILTER (wav_trips_timed >= 30), count(*) FILTER (wav_trips_timed < 30)
                   FROM wav_zone""").fetchall()[0]
    check("HVFHV trips, Sept 2026", f"{r[0]:,}", f"{r[1]:,} WAV-requested (completed trips only — no 'match rate' is computable)")
    check("WAV trips with a usable wait (0–3600 s)", f"{r[2]:,}", f"{r[3]} zones have >=30 such trips; {r[4]} zones marked 'too few rides'")

    # zone → zone ride-hail price and duration (all HVFHV trips: WAV and standard trips price alike per mile)
    con.sql("""
        CREATE TABLE od_fare AS
        SELECT PULocationID::INT AS pu_zone, DOLocationID::INT AS do_zone, count(*) AS trips,
               round(median(fare), 2) AS fare_median, round(quantile_cont(fare, 0.9), 2) AS fare_p90,
               round(median(trip_time) / 60.0, 1) AS minutes_median, round(median(trip_miles), 2) AS miles_median
        FROM (SELECT PULocationID, DOLocationID, trip_time, trip_miles,
                     base_passenger_fare + tolls + bcf + sales_tax + congestion_surcharge + airport_fee
                       + coalesce(cbd_congestion_fee, 0) AS fare
              FROM 'data/fhvhv_2026-09.parquet' WHERE base_passenger_fare > 0)
        GROUP BY 1, 2 HAVING count(*) >= 20
    """)
    r = con.sql("SELECT count(*), sum(trips) FROM od_fare").fetchall()[0]
    check("zone → zone ride-hail fares (pairs with >= 20 trips)", f"{r[0]:,} pairs", f"{r[1]:,} trips; fare = passenger fare + tolls, taxes and surcharges, before tip")
    r = con.sql("""SELECT round(median(base_passenger_fare / trip_miles) FILTER (wav_request_flag = 'Y'), 2),
                          round(median(base_passenger_fare / trip_miles) FILTER (wav_request_flag = 'N'), 2)
                   FROM 'data/fhvhv_2026-09.parquet' WHERE trip_miles > 1 AND base_passenger_fare > 0""").fetchall()[0]
    check("fare per mile, WAV vs standard trips (USD)", f"{r[0]} vs {r[1]}", "close enough that all trips are used to price a zone → zone ride")

    con.sql("""
        CREATE TABLE city_stats AS
        SELECT median(w) FILTER (wav_request_flag = 'Y') / 60.0 AS wav_wait_median_min,
               median(w) FILTER (wav_request_flag = 'N') / 60.0 AS other_wait_median_min,
               quantile_cont(w, 0.9) FILTER (wav_request_flag = 'Y') / 60.0 AS wav_wait_p90_min,
               quantile_cont(w, 0.9) FILTER (wav_request_flag = 'N') / 60.0 AS other_wait_p90_min
        FROM (SELECT wav_request_flag, date_diff('second', request_datetime, on_scene_datetime) AS w
              FROM 'data/fhvhv_2026-09.parquet')
        WHERE w BETWEEN 0 AND 3600
    """)
    r = con.sql("SELECT round(wav_wait_median_min, 2), round(other_wait_median_min, 2), round(wav_wait_p90_min, 1), round(other_wait_p90_min, 1) FROM city_stats").fetchall()[0]
    check("citywide median pickup wait: WAV vs other riders (min)", f"{r[0]} vs {r[1]}",
          f"p90 {r[2]} vs {r[3]}; research notes said 3.8 vs 3.6 — the WAV figure there did not reproduce", True)

    # ------------------------------------------------------- zone ↔ stations
    # "Reach" of a zone = its accessible stations: inside the zone polygon, or within 800 m of the
    # zone's residential centre (unit-weighted centroid of its multifamily lots; geometric centroid if none).
    con.sql("""
        CREATE TABLE zone_home AS
        SELECT z.zone_id,
               coalesce(ST_Point(h.x, h.y), z.centroid_ft) AS home_ft,
               h.x IS NOT NULL AS home_is_residential
        FROM zones z LEFT JOIN (
            SELECT zone_id, sum(ST_X(pt_ft) * unitsres) / sum(unitsres) AS x,
                            sum(ST_Y(pt_ft) * unitsres) / sum(unitsres) AS y
            FROM lots WHERE zone_id IS NOT NULL GROUP BY zone_id) h USING (zone_id)
    """)
    con.sql("""
        CREATE TABLE zone_stations AS
        SELECT z.zone_id, s.station_id, s.complex_id, s.stop_name, s.routes, s.ada, s.ada_notes, s.lat, s.lon,
               s.elevator_uptime, s.uptime_worst_elevator, s.n_elevators,
               round(ST_Distance(h.home_ft, s.pt_ft) * 0.3048)::INT AS dist_m,
               ST_Contains(z.geom_ft, s.pt_ft) AS inside_zone
        FROM zones z JOIN zone_home h USING (zone_id)
        JOIN stations s ON s.ada IN (1,2)
         AND (ST_Contains(z.geom_ft, s.pt_ft) OR ST_DWithin(h.home_ft, s.pt_ft, getvariable('ft800')))
    """)

    # --------------------------------------------------------- zone_features
    con.sql(f"""
        CREATE TABLE zone_features AS
        WITH housing AS (
            SELECT l.zone_id,
                   count(*) AS mf_lots,
                   sum(unitsres) AS mf_units,
                   sum(unitsres) FILTER (yearbuilt >= 1991) AS fha_units,
                   sum(unitsres) FILTER (bldgclass LIKE 'D%') AS elevator_units,
                   sum(unitsres) FILTER (a.bbl IS NOT NULL) AS units_near_ada,
                   sum(unitsres * zo.zori) / nullif(sum(unitsres) FILTER (zo.zori IS NOT NULL), 0) AS rent_zori,
                   coalesce(sum(unitsres) FILTER (zo.zori IS NOT NULL), 0) / sum(unitsres) AS rent_unit_coverage
            FROM lots l
            LEFT JOIN zori zo USING (zipcode)
            LEFT JOIN lot_ada a USING (bbl)
            WHERE l.zone_id IS NOT NULL
            GROUP BY l.zone_id),
        st AS (
            SELECT zone_id, count(*) AS ada_stations,
                   count(*) FILTER (ada = 1) AS ada_stations_full,
                   string_agg(stop_name || ' (' || routes || ')', '; ' ORDER BY dist_m) AS ada_station_names,
                   avg(elevator_uptime) AS ada_elevator_uptime,
                   min(elevator_uptime) AS ada_elevator_uptime_worst,
                   min(dist_m) AS nearest_ada_m
            FROM zone_stations GROUP BY zone_id),
        base AS (
            SELECT z.zone_id, z.zone_name, z.borough, z.service_zone,
                   round(rent_zori)::INT AS rent_zori,
                   round(rent_unit_coverage, 3) AS rent_unit_coverage,
                   coalesce(mf_units, 0) AS mf_units,
                   round(coalesce(fha_units, 0) / nullif(mf_units, 0), 4) AS fha_units_share,
                   round(coalesce(elevator_units, 0) / nullif(mf_units, 0), 4) AS elevator_units_share,
                   round(coalesce(units_near_ada, 0) / nullif(mf_units, 0), 4) AS units_near_ada_share,
                   coalesce(st.ada_stations, 0) AS ada_stations_800m,
                   coalesce(st.ada_stations_full, 0) AS ada_stations_full,
                   st.ada_station_names,
                   round(st.ada_elevator_uptime, 4) AS ada_elevator_uptime,
                   round(st.ada_elevator_uptime_worst, 4) AS ada_elevator_uptime_worst,
                   st.nearest_ada_m,
                   coalesce(w.wav_trips, 0) AS wav_trips,
                   CASE WHEN w.wav_trips_timed >= {MIN_WAV_TRIPS} THEN round(w.wav_wait_median_min, 2) END AS wav_wait_median_min,
                   CASE WHEN w.wav_trips_timed >= {MIN_WAV_TRIPS} THEN round(w.wav_wait_p90_min, 2) END AS wav_wait_p90_min,
                   coalesce(w.wav_trips_timed, 0) < {MIN_WAV_TRIPS} AS wav_too_few_rides,
                   round(1000.0 * w.wav_trips / nullif(w.trips_all, 0), 2) AS wav_trips_per_1000,
                   coalesce(w.access_a_ride_trips, 0) AS access_a_ride_trips,
                   coalesce(r.ramps, 0) AS ramps,
                   round(r.ramps_good / nullif(r.ramps, 0), 4) AS ramp_good_share
            FROM zones z
            LEFT JOIN housing USING (zone_id)
            LEFT JOIN st USING (zone_id)
            LEFT JOIN wav_zone w USING (zone_id)
            LEFT JOIN ramp_zone r USING (zone_id))
        SELECT *,
               -- city percentiles (0..1) among zones that have the value; NULL stays NULL (never imputed)
               CASE WHEN fha_units_share IS NOT NULL THEN round(percent_rank() OVER (PARTITION BY fha_units_share IS NOT NULL ORDER BY fha_units_share), 4) END AS fha_pct,
               CASE WHEN elevator_units_share IS NOT NULL THEN round(percent_rank() OVER (PARTITION BY elevator_units_share IS NOT NULL ORDER BY elevator_units_share), 4) END AS elevator_pct,
               CASE WHEN ramp_good_share IS NOT NULL THEN round(percent_rank() OVER (PARTITION BY ramp_good_share IS NOT NULL ORDER BY ramp_good_share), 4) END AS ramp_pct,
               CASE WHEN wav_wait_median_min IS NOT NULL THEN round(percent_rank() OVER (PARTITION BY wav_wait_median_min IS NOT NULL ORDER BY wav_wait_median_min DESC), 4) END AS wav_pct
        FROM base
        ORDER BY zone_id
    """)
    r = con.sql("""SELECT count(*), count(rent_zori), count(*) FILTER (ada_stations_800m > 0), count(wav_wait_median_min),
                          count(ramp_good_share), count(fha_units_share) FROM zone_features""").fetchall()[0]
    check("zone_features rows", r[0], f"rent: {r[1]} zones, >=1 ADA station in reach: {r[2]}, WAV wait: {r[3]}, ramps: {r[4]}, housing: {r[5]}", r[0] == 263)
    r = con.sql("SELECT count(*) FROM zone_features WHERE rent_zori IS NULL").fetchall()[0]
    check("zones with no rent data", r[0], "shown as 'no rent data', never imputed")
    r = con.sql("""SELECT round(median(wav_wait_median_min),2), round(min(wav_wait_median_min),2), round(max(wav_wait_median_min),2)
                   FROM zone_features""").fetchall()[0]
    check("WAV median wait across zones (min)", r[0], f"range {r[1]} – {r[2]} min (research notes measured ~1 to 6.6)")

    con.execute("CREATE TABLE data_checks AS SELECT * FROM (SELECT unnest($1::STRUCT(name VARCHAR, value VARCHAR, detail VARCHAR, ok BOOLEAN)[], recursive := true))",
                [[{**c, "value": str(c["value"])} for c in checks]])

    import build_extra
    build_extra.run(con, check)
    build_extra.run_changes(con, check)
    con.execute("CREATE OR REPLACE TABLE data_checks AS SELECT * FROM (SELECT unnest($1::STRUCT(name VARCHAR, value VARCHAR, detail VARCHAR, ok BOOLEAN)[], recursive := true))",
                [[{**c, "value": str(c["value"])} for c in checks]])

    # ------------------------------------------------------ exports for the UI
    con.sql("COPY zone_features TO 'data/zone_features.parquet' (FORMAT parquet)")
    rows = con.sql("""
        SELECT zone_id, zone_name, borough,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(geom_ft, 60), 'EPSG:2263', 'EPSG:4326', always_xy := true)) AS gj,
               ST_X(ST_Transform(ST_PointOnSurface(geom_ft), 'EPSG:2263', 'EPSG:4326', always_xy := true)) AS lon,
               ST_Y(ST_Transform(ST_PointOnSurface(geom_ft), 'EPSG:2263', 'EPSG:4326', always_xy := true)) AS lat
        FROM zones ORDER BY zone_id
    """).fetchall()

    def rnd(o):
        if isinstance(o, list):
            return [rnd(x) for x in o]
        return round(o, 5)

    feats = []
    for zid, name, boro, gj, lon, lat in rows:
        g = json.loads(gj)
        g["coordinates"] = rnd(g["coordinates"])
        feats.append({"type": "Feature", "geometry": g,
                      "properties": {"zone_id": zid, "zone_name": name, "borough": boro,
                                     "label_lon": round(lon, 5), "label_lat": round(lat, 5)}})
    (DATA / "zones.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    check("zones.geojson written (EPSG:2263 → 4326)", f"{(DATA / 'zones.geojson').stat().st_size // 1024} KB", "simplified to ~18 m for the web map")

    # drop geometry-heavy helper tables the agent never needs; keep geometry in zones/stations for point lookups
    for t in ("lots_raw", "stations_raw", "ramps", "lot_ada"):
        con.sql(f"DROP TABLE {t}")
    con.sql("ALTER TABLE lots DROP COLUMN pt_ft")

    print("\n5 sample zones:")
    con.sql("""SELECT zone_id, zone_name, borough, rent_zori, fha_units_share AS fha, elevator_units_share AS elev,
                      ada_stations_800m AS ada_st, ada_elevator_uptime AS uptime, wav_wait_median_min AS wav_min, ramp_good_share AS ramps
               FROM zone_features WHERE zone_id IN (4, 25, 161, 168, 236) ORDER BY zone_id""").show()
    con.close()
    print(f"built {DB_PATH.relative_to(ROOT)} in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
