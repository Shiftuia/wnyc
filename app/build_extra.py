"""Enrichment layers (data/extra/, fetched by scripts/fetch_extra.sh) and the accessible-subway graph.

Called from build_data.py. Adds tables: zone_extra, od_fare_period, ride_model, subway_edges,
subway_transfers, route_headway, station_gtfs. None of these change the 0–100 score.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from pathlib import Path

X = "data/extra"
PT = "ST_Transform(ST_Point({lon}::DOUBLE, {lat}::DOUBLE), 'EPSG:4326', 'EPSG:2263', always_xy := true)"


def _zone_counts(con, name: str, src_sql: str, lat: str, lon: str, aggs: str) -> None:
    """Point-in-polygon aggregate of a lat/lon source to taxi zones → temp table x_<name>."""
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE x_{name} AS
        SELECT z.zone_id, {aggs}
        FROM ({src_sql}) s JOIN zones z
          ON ST_Contains(z.geom_ft, {PT.format(lon='s.' + lon, lat='s.' + lat)})
        GROUP BY z.zone_id
    """)


def _elevation(con, check) -> None:
    """Hilliness proxy: elevation range across up to 6 residential lots per zone (Open-Meteo, keyless)."""
    cache = Path(X) / "elevation.json"
    pts = con.sql("""
        SELECT zone_id, round(lat, 5), round(lon, 5) FROM (
            SELECT zone_id, lat, lon, row_number() OVER (PARTITION BY zone_id ORDER BY hash(bbl)) AS rn
            FROM lots WHERE zone_id IS NOT NULL AND lat IS NOT NULL) WHERE rn <= 6 ORDER BY zone_id, rn
    """).fetchall()
    elev: dict[str, float] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [p for p in pts if f"{p[1]},{p[2]}" not in elev]
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            url = ("https://api.open-meteo.com/v1/elevation?latitude=" + ",".join(str(p[1]) for p in chunk)
                   + "&longitude=" + ",".join(str(p[2]) for p in chunk))
            req = urllib.request.Request(url, headers={"User-Agent": "wheelchair-nyc-demo/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                vals = json.loads(r.read().decode())["elevation"]
            for p, v in zip(chunk, vals):
                elev[f"{p[1]},{p[2]}"] = v
            cache.write_text(json.dumps(elev))
            time.sleep(12)   # the free tier meters every coordinate; stay under its per-minute limit
        cache.write_text(json.dumps(elev))
    except Exception as e:
        check("elevation samples (Open-Meteo)", "partial", f"{type(e).__name__} — hilliness left as 'no data' where missing", False)
    rows = [(p[0], elev[f"{p[1]},{p[2]}"]) for p in pts if f"{p[1]},{p[2]}" in elev]
    con.execute("CREATE OR REPLACE TEMP TABLE x_elev_pts (zone_id INT, elev_m DOUBLE)")
    if rows:
        con.executemany("INSERT INTO x_elev_pts VALUES (?, ?)", rows)
    con.sql("""CREATE OR REPLACE TEMP TABLE x_elev AS
               SELECT zone_id, count(*) AS elev_samples, round(max(elev_m) - min(elev_m), 1) AS elev_range_m,
                      round(avg(elev_m), 1) AS elev_mean_m
               FROM x_elev_pts GROUP BY zone_id HAVING count(*) >= 3""")
    n = con.sql("SELECT count(*), round(median(elev_range_m), 1), max(elev_range_m) FROM x_elev").fetchall()[0]
    check("hilliness: elevation range over <=6 residential lots per zone", f"{n[0]} zones",
          f"median range {n[1]} m, max {n[2]} m ({len(rows)} sampled points) — a rough proxy, not street slope")


def run(con, check) -> None:
    # ------------------------------------------------------------ point layers
    _zone_counts(con, "hpd", f"SELECT * FROM read_csv('{X}/hpd_elevator.csv', all_varchar = true)", "latitude", "longitude",
                 "count(DISTINCT s.bbl) AS hpd_elev_buildings, sum(least(s.n::INT, 5)) AS hpd_elev_complaints")
    r = con.sql(f"SELECT (SELECT count(*) FROM read_csv('{X}/hpd_elevator.csv', all_varchar = true)), sum(hpd_elev_buildings), sum(hpd_elev_complaints) FROM x_hpd").fetchall()[0]
    check("HPD elevator complaints, last 12 months", f"{r[0]:,} buildings", f"{r[1]:,} joined to zones; complaints capped at 5 per building → {r[2]:,}")

    _zone_counts(con, "lot", f"""SELECT * FROM read_csv('{X}/lotteries.csv', all_varchar = true)
                                 WHERE try_cast(latitude AS DOUBLE) IS NOT NULL""", "latitude", "longitude",
                 """count(*) FILTER (s.lottery_status = 'Active') AS lottery_active,
                    coalesce(sum(s.unit_count::INT) FILTER (s.lottery_status = 'Active'), 0) AS lottery_active_units,
                    coalesce(round(sum(s.unit_count::INT * coalesce(try_cast(s.lottery_mobility_percent AS DOUBLE), 5) / 100.0)
                                   FILTER (s.lottery_status = 'Active')), 0) AS lottery_active_mobility_units,
                    coalesce(sum(s.unit_count::INT) FILTER (s.lottery_start_date >= '2023-11-01'), 0) AS lottery_units_3y,
                    string_agg(s.lottery_name, '; ') FILTER (s.lottery_status = 'Active') AS lottery_active_names""")
    r = con.sql(f"""SELECT count(*), count(*) FILTER (try_cast(latitude AS DOUBLE) IS NOT NULL),
                           count(*) FILTER (lottery_status = 'Active'), count(*) FILTER (lottery_mobility_percent IS NOT NULL)
                    FROM read_csv('{X}/lotteries.csv', all_varchar = true)""").fetchall()[0]
    check("Housing Connect lotteries", f"{r[0]:,}", f"{r[1]:,} with coordinates, {r[2]} active now; mobility % filled on {r[3]} (5% assumed elsewhere)")

    _zone_counts(con, "bus", f"SELECT * FROM read_csv('{X}/bus_stops.csv', all_varchar = true)", "lat", "lon",
                 "count(DISTINCT s.stop_id) AS bus_stops, count(DISTINCT s.route_id) AS bus_routes")
    r = con.sql(f"SELECT (SELECT count(DISTINCT stop_id) FROM read_csv('{X}/bus_stops.csv', all_varchar = true)), sum(bus_stops) FROM x_bus").fetchall()[0]
    check("MTA bus stops in effect", f"{r[0]:,}", f"{r[1]:,} joined to zones (every NYC bus has a ramp or lift)")

    _zone_counts(con, "ped", f"SELECT * FROM read_csv('{X}/ped_crashes.csv', all_varchar = true)", "latitude", "longitude",
                 "sum(s.number_of_pedestrians_injured::INT + s.number_of_pedestrians_killed::INT) AS ped_injuries")
    r = con.sql("SELECT sum(ped_injuries) FROM x_ped").fetchall()[0]
    check("pedestrians injured or killed in crashes, last 12 months", f"{r[0]:,}", "joined to zones; shown per km², not scored")

    _zone_counts(con, "wc", f"""SELECT * FROM read_csv('{X}/restrooms.csv', all_varchar = true)
                                WHERE accessibility = 'Fully Accessible'""", "latitude", "longitude",
                 "count(*) AS restrooms_accessible")
    _zone_counts(con, "aps", f"SELECT * FROM read_csv('{X}/aps.csv', all_varchar = true)", "point_y", "point_x", "count(*) AS aps")
    _zone_counts(con, "fel", f"SELECT * FROM read_csv('{X}/felonies.csv', all_varchar = true)", "latitude", "longitude", "count(*) AS felonies")
    r = con.sql(f"SELECT (SELECT count(*) FROM read_csv('{X}/felonies.csv', all_varchar = true)), sum(felonies) FROM x_fel").fetchall()[0]
    check("reported felonies, 2026 year to date (opt-in display only, never scored)", f"{r[0]:,}", f"{r[1]:,} joined to zones")
    _zone_counts(con, "clin", f"""SELECT * FROM read_csv('{X}/health.csv', all_varchar = true)
                                  WHERE try_cast(latitude AS DOUBLE) IS NOT NULL""", "latitude", "longitude",
                 "count(*) FILTER (s.description <> 'Hospital') AS clinics, count(*) FILTER (s.description = 'Hospital') AS hospitals")

    # nearest hospital to the zone's residential centre
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE x_hosp AS
        SELECT zone_id, arg_min(facility_name, d) AS hospital_nearest, round(min(d) * 0.3048 / 1000, 1) AS hospital_nearest_km
        FROM (SELECT h.zone_id, s.facility_name,
                     ST_Distance(h.home_ft, {PT.format(lon='s.longitude', lat='s.latitude')}) AS d
              FROM zone_home h, read_csv('{X}/health.csv', all_varchar = true) s
              WHERE s.description = 'Hospital' AND try_cast(s.latitude AS DOUBLE) IS NOT NULL)
        GROUP BY zone_id
    """)

    # CDC PLACES by ZCTA → zone, weighted by residential units of the lots in each ZIP
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE x_places AS
        SELECT l.zone_id,
               round(sum(l.unitsres * p.mobility_crudeprev::DOUBLE) / sum(l.unitsres), 1) AS mobility_disability_pct,
               round(sum(l.unitsres * p.lacktrpt_crudeprev::DOUBLE) / sum(l.unitsres), 1) AS lack_transport_pct,
               round(sum(l.unitsres * p.loneliness_crudeprev::DOUBLE) / sum(l.unitsres), 1) AS loneliness_pct
        FROM lots l JOIN read_csv('{X}/places_zcta.csv', all_varchar = true) p ON p.zcta5 = l.zipcode
        WHERE l.zone_id IS NOT NULL AND p.mobility_crudeprev IS NOT NULL
        GROUP BY l.zone_id
    """)
    r = con.sql("""SELECT (SELECT count(DISTINCT zipcode) FROM lots WHERE zone_id IS NOT NULL),
                          (SELECT count(*) FROM x_places)""").fetchall()[0]
    check("CDC PLACES mobility-disability prevalence (ZIP → zone)", f"{r[1]} zones", "model-based estimates; context only, never scored")

    _elevation(con, check)

    con.sql("""
        CREATE OR REPLACE TABLE zone_extra AS
        WITH e AS (SELECT zone_id, sum(unitsres) AS elevator_units FROM lots WHERE bldgclass LIKE 'D%' AND zone_id IS NOT NULL GROUP BY 1),
        b AS (
            SELECT z.zone_id,
                   round(ST_Area(z.geom_ft) * 0.3048 * 0.3048 / 1e6, 3) AS area_km2,
                   round(ST_Y(ST_Transform(h.home_ft, 'EPSG:2263', 'EPSG:4326', always_xy := true)), 6) AS home_lat,
                   round(ST_X(ST_Transform(h.home_ft, 'EPSG:2263', 'EPSG:4326', always_xy := true)), 6) AS home_lon,
                   coalesce(x_hpd.hpd_elev_buildings, 0) AS hpd_elev_buildings,
                   coalesce(x_hpd.hpd_elev_complaints, 0) AS hpd_elev_complaints,
                   CASE WHEN e.elevator_units >= 500 THEN round(1000.0 * coalesce(x_hpd.hpd_elev_complaints, 0) / e.elevator_units, 2) END AS elev_complaints_per_1000_units,
                   coalesce(lottery_active, 0) AS lottery_active, coalesce(lottery_active_units, 0) AS lottery_active_units,
                   coalesce(lottery_active_mobility_units, 0)::INT AS lottery_active_mobility_units,
                   coalesce(lottery_units_3y, 0) AS lottery_units_3y, lottery_active_names,
                   coalesce(bus_stops, 0) AS bus_stops, coalesce(bus_routes, 0) AS bus_routes,
                   coalesce(ped_injuries, 0) AS ped_injuries,
                   coalesce(restrooms_accessible, 0) AS restrooms_accessible,
                   coalesce(aps, 0) AS aps, coalesce(felonies, 0) AS felonies,
                   coalesce(clinics, 0) AS clinics, coalesce(hospitals, 0) AS hospitals,
                   hospital_nearest, hospital_nearest_km,
                   mobility_disability_pct, lack_transport_pct, loneliness_pct,
                   elev_range_m, elev_mean_m
            FROM zones z JOIN zone_home h USING (zone_id)
            LEFT JOIN e USING (zone_id) LEFT JOIN x_hpd USING (zone_id) LEFT JOIN x_lot USING (zone_id)
            LEFT JOIN x_bus USING (zone_id) LEFT JOIN x_ped USING (zone_id) LEFT JOIN x_wc USING (zone_id)
            LEFT JOIN x_aps USING (zone_id) LEFT JOIN x_fel USING (zone_id) LEFT JOIN x_clin USING (zone_id)
            LEFT JOIN x_hosp USING (zone_id) LEFT JOIN x_places USING (zone_id) LEFT JOIN x_elev USING (zone_id)),
        d AS (SELECT *, round(bus_stops / area_km2, 1) AS bus_stops_per_km2, round(ped_injuries / area_km2, 1) AS ped_injuries_per_km2,
                     round(aps / area_km2, 1) AS aps_per_km2, round(felonies / area_km2, 1) AS felonies_per_km2 FROM b)
        SELECT d.*,
               -- city percentiles among zones with housing; "better" direction noted per column
               round(percent_rank() OVER (ORDER BY bus_stops_per_km2), 3) AS bus_pct,                 -- higher = more stops
               round(percent_rank() OVER (ORDER BY ped_injuries_per_km2 DESC), 3) AS ped_safe_pct,    -- higher = fewer injuries
               round(percent_rank() OVER (ORDER BY felonies_per_km2 DESC), 3) AS felony_low_pct,      -- higher = fewer reports
               CASE WHEN elev_complaints_per_1000_units IS NOT NULL THEN
                    round(percent_rank() OVER (PARTITION BY elev_complaints_per_1000_units IS NOT NULL ORDER BY elev_complaints_per_1000_units DESC), 3) END AS elev_ok_pct,
               CASE WHEN elev_range_m IS NOT NULL THEN
                    round(percent_rank() OVER (PARTITION BY elev_range_m IS NOT NULL ORDER BY elev_range_m DESC), 3) END AS flat_pct
        FROM d ORDER BY zone_id
    """)
    r = con.sql("SELECT count(*), count(*) FILTER (lottery_active > 0), sum(lottery_active_units), count(elev_complaints_per_1000_units), count(mobility_disability_pct), count(elev_range_m) FROM zone_extra").fetchall()[0]
    check("zone_extra rows", r[0], f"{r[1]} zones with an open lottery ({r[2]:,} units); elevator-complaint rate for {r[3]} zones; CDC context for {r[4]}; hilliness for {r[5]}", r[0] == 263)

    # ------------------------------------------------ ride-hail by time of day
    con.sql("""
        CREATE OR REPLACE TABLE od_fare_period AS
        SELECT PULocationID::INT AS pu_zone, DOLocationID::INT AS do_zone, period, count(*) AS trips,
               round(median(fare), 2) AS fare_median, round(median(trip_time) / 60.0, 1) AS minutes_median
        FROM (SELECT PULocationID, DOLocationID, trip_time,
                     base_passenger_fare + tolls + bcf + sales_tax + congestion_surcharge + airport_fee + coalesce(cbd_congestion_fee, 0) AS fare,
                     CASE WHEN dayofweek(pickup_datetime) IN (0, 6) THEN 'weekend'
                          WHEN hour(pickup_datetime) BETWEEN 7 AND 9 THEN 'am_peak'
                          WHEN hour(pickup_datetime) BETWEEN 10 AND 15 THEN 'midday'
                          WHEN hour(pickup_datetime) BETWEEN 16 AND 18 THEN 'pm_peak'
                          ELSE 'night' END AS period
              FROM 'data/fhvhv_2026-09.parquet' WHERE base_passenger_fare > 0)
        GROUP BY 1, 2, 3 HAVING count(*) >= 10
    """)
    con.sql("""
        CREATE OR REPLACE TABLE ride_model AS
        SELECT period, count(*) AS trips,
               regr_intercept(fare, trip_miles) AS fare_base, regr_slope(fare, trip_miles) AS fare_per_mile,
               regr_intercept(trip_time / 60.0, trip_miles) AS min_base, regr_slope(trip_time / 60.0, trip_miles) AS min_per_mile,
               median(trip_miles / nullif(trip_time, 0) * 3600) AS mph_median
        FROM (SELECT trip_miles, trip_time,
                     base_passenger_fare + tolls + bcf + sales_tax + congestion_surcharge + airport_fee + coalesce(cbd_congestion_fee, 0) AS fare,
                     CASE WHEN dayofweek(pickup_datetime) IN (0, 6) THEN 'weekend'
                          WHEN hour(pickup_datetime) BETWEEN 7 AND 9 THEN 'am_peak'
                          WHEN hour(pickup_datetime) BETWEEN 10 AND 15 THEN 'midday'
                          WHEN hour(pickup_datetime) BETWEEN 16 AND 18 THEN 'pm_peak'
                          ELSE 'night' END AS period
              FROM 'data/fhvhv_2026-09.parquet'
              WHERE base_passenger_fare > 0 AND trip_miles BETWEEN 0.3 AND 30 AND trip_time BETWEEN 120 AND 7200)
        GROUP BY period
    """)
    rows = con.sql("SELECT period, round(mph_median, 1), round(fare_base, 2), round(fare_per_mile, 2) FROM ride_model ORDER BY period").fetchall()
    n = con.sql("SELECT count(*) FROM od_fare_period").fetchall()[0][0]
    check("ride-hail speed by time of day (mph, median)", ", ".join(f"{p} {m}" for p, m, _, _ in rows),
          f"{n:,} zone-pair × period rows; fallback price model fare ≈ base + per-mile fitted per period")

    # ------------------------------------------------------------ subway graph
    g = f"{X}/gtfs"
    con.sql(f"""
        CREATE OR REPLACE TABLE station_gtfs AS
        SELECT station_id::INT AS station_id, gtfs_stop_id FROM read_csv('data/subway_stations.csv', all_varchar = true)
    """)
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE st AS
        SELECT t.route_id, s.trip_id, s.stop_sequence::INT AS seq, left(s.stop_id, length(s.stop_id) - 1) AS stop,
               right(s.stop_id, 1) AS dir,
               split_part(s.departure_time, ':', 1)::INT * 3600 + split_part(s.departure_time, ':', 2)::INT * 60 + split_part(s.departure_time, ':', 3)::INT AS dep,
               split_part(s.arrival_time, ':', 1)::INT * 3600 + split_part(s.arrival_time, ':', 2)::INT * 60 + split_part(s.arrival_time, ':', 3)::INT AS arr
        FROM read_csv('{g}/stop_times.txt', all_varchar = true) s
        JOIN read_csv('{g}/trips.txt', all_varchar = true) t USING (trip_id)
        WHERE t.service_id = 'Weekday'
    """)
    con.sql("""
        CREATE OR REPLACE TABLE subway_edges AS
        SELECT route_id AS route, stop AS from_stop, nxt AS to_stop, round(median(secs))::INT AS secs, count(*) AS trips
        FROM (SELECT route_id, stop, dep, lead(stop) OVER w AS nxt, lead(arr) OVER w - dep AS secs
              FROM st WHERE dep BETWEEN 6 * 3600 AND 21 * 3600
              WINDOW w AS (PARTITION BY trip_id ORDER BY seq))
        WHERE nxt IS NOT NULL AND secs > 0
        GROUP BY 1, 2, 3 HAVING count(*) >= 10       -- drop rare reroutes
    """)
    con.sql("""
        CREATE OR REPLACE TABLE route_headway AS
        SELECT route, round(median(headway_min), 1) AS headway_min
        FROM (SELECT route_id AS route, stop, dir, 900.0 / count(*) AS headway_min   -- 06:00–21:00 = 900 minutes
              FROM st WHERE dep BETWEEN 6 * 3600 AND 21 * 3600 GROUP BY 1, 2, 3 HAVING count(*) >= 15)
        GROUP BY route
    """)
    con.sql(f"""
        CREATE OR REPLACE TABLE subway_transfers AS
        SELECT from_stop_id AS from_stop, to_stop_id AS to_stop, min_transfer_time::INT AS secs
        FROM read_csv('{g}/transfers.txt', all_varchar = true) WHERE from_stop_id <> to_stop_id
    """)
    con.sql(f"""
        CREATE OR REPLACE TABLE subway_shapes AS
        SELECT t.route, s.shape_id, s.shape_pt_sequence::INT AS seq, round(s.shape_pt_lat::DOUBLE, 6) AS lat, round(s.shape_pt_lon::DOUBLE, 6) AS lon
        FROM read_csv('{g}/shapes.txt', all_varchar = true) s
        JOIN (SELECT DISTINCT route_id AS route, shape_id FROM read_csv('{g}/trips.txt', all_varchar = true) WHERE service_id = 'Weekday') t USING (shape_id)
    """)
    r = con.sql("SELECT count(DISTINCT shape_id), count(*) FROM subway_shapes").fetchall()[0]
    check("subway track geometry (GTFS shapes, weekday)", f"{r[0]} track shapes", f"{r[1]:,} points — used only to draw routes along the real tracks")
    r = con.sql("""SELECT (SELECT count(*) FROM subway_edges), (SELECT count(DISTINCT route) FROM subway_edges),
                          (SELECT count(*) FROM subway_transfers),
                          (SELECT count(*) FROM station_gtfs),
                          (SELECT count(*) FROM station_gtfs WHERE gtfs_stop_id IN (SELECT from_stop FROM subway_edges UNION SELECT to_stop FROM subway_edges))""").fetchall()[0]
    check("subway timetable graph (MTA GTFS, weekday 06:00–21:00)", f"{r[0]:,} links on {r[1]} routes", f"{r[2]} station-to-station transfers")
    check("JOIN stations → timetable stops", f"{r[4]}/{r[3]}", f"{100 * r[4] / r[3]:.1f}% matched on gtfs_stop_id", r[4] / r[3] >= 0.95)
    hw = con.sql("SELECT round(min(headway_min), 1), round(median(headway_min), 1), round(max(headway_min), 1) FROM route_headway").fetchall()[0]
    check("route headways (min between trains, daytime)", f"median {hw[1]}", f"range {hw[0]} – {hw[2]}")


# Stations named in MTA capital projects that add accessibility (data.ny.gov 9hy6-8j6t, read 2026-11-23).
# The dataset has no station ids, so names were typed from the project descriptions; forecasts are the MTA's own
# and several earlier ones have already slipped. None = in procurement, no date published.
UPGRADES = [
    ("Tremont Av", "B", "2026-07-31", "149 St Complex & Tremont Av"), ("149 St-Grand Concourse", "4", "2026-07-31", "149 St Complex & Tremont Av"),
    ("149 St-Grand Concourse", "2", "2026-07-31", "149 St Complex & Tremont Av"),
    ("Borough Hall", "4", "2026-08-06", "Borough Hall"),
    ("Bay Ridge-95 St", "R", "2026-09-30", "Package 4"), ("137 St-City College", "1", "2026-09-30", "Package 4"),
    ("Northern Blvd", "M", "2026-09-30", "Package 4"), ("Parkchester", "6", "2026-09-30", "Package 4"),
    ("Broadway Junction", "A", "2027-09-07", "Broadway Junction"), ("Broadway Junction", "J", "2027-09-07", "Broadway Junction"),
    ("Broadway Junction", "L", "2027-09-07", "Broadway Junction"),
    ("167 St", "B", "2028-08-03", "Package 7"), ("Kingsbridge Rd", "4", "2028-08-03", "Package 7"),
    ("Norwood Av", "J", "2028-10-11", "Package 6"), ("Myrtle Av", "J", "2028-10-11", "Package 6"), ("Avenue I", "F", "2028-10-11", "Package 6"),
    ("Burnside Av", "4", "2028-10-11", "Package 6"), ("Middletown Rd", "6", "2028-10-11", "Package 6"),
    ("42 St-Bryant Pk", "B", "2028-11-30", "Bryant Park complex"), ("5 Av", "7", "2028-11-30", "Bryant Park complex"),
    ("Parsons Blvd", "F", "2028-12-30", "Package 9"), ("Briarwood", "E", "2028-12-30", "Package 9"), ("Gates Av", "J", "2028-12-30", "Package 9"),
    ("18 Av", "D", None, "Package 8"), ("Fort Hamilton Pkwy", "D", None, "Package 8"), ("Jefferson St", "L", None, "Package 8"),
    ("Neptune Av", "F", None, "Package 8"), ("Nostrand Av", "A", None, "Package 8"),
    ("2 Av", "F", None, "Package 10"), ("7 Av", "B", None, "Package 10"), ("Morrison Av-Soundview", "6", None, "Package 10"),
    ("110 St", "6", None, "Package 11"), ("145 St", "A", None, "Package 11"), ("Wakefield-241 St", "2", None, "Package 11"),
    ("Brook Av", "6", None, "Brook Av & 3 Av-138 St"), ("3 Av-138 St", "6", None, "Brook Av & 3 Av-138 St"),
]


def run_changes(con, check) -> None:
    """What is about to change: live/planned elevator outages and stations due to become accessible."""
    import datetime as dt
    import os

    # ---- stations becoming accessible
    con.execute("CREATE OR REPLACE TEMP TABLE up_raw (stop_name VARCHAR, route VARCHAR, forecast DATE, project VARCHAR)")
    con.executemany("INSERT INTO up_raw VALUES (?, ?, ?, ?)", UPGRADES)
    con.sql("""
        CREATE OR REPLACE TABLE station_upgrades AS
        SELECT s.station_id, s.complex_id, s.stop_name, s.routes, s.ada AS ada_today, u.forecast, u.project, s.lat, s.lon, s.zone_id,
               s.pt_ft
        FROM up_raw u JOIN stations s ON s.stop_name = u.stop_name AND list_contains(string_split(s.routes, ' '), u.route)
    """)
    r = con.sql("""SELECT (SELECT count(*) FROM up_raw), (SELECT count(DISTINCT (stop_name, route)) FROM up_raw u
                          WHERE EXISTS (SELECT 1 FROM stations s WHERE s.stop_name = u.stop_name AND list_contains(string_split(s.routes, ' '), u.route))),
                          (SELECT count(*) FROM station_upgrades), (SELECT count(*) FROM station_upgrades WHERE ada_today = 1)""").fetchall()[0]
    check("stations due to become accessible (typed from MTA capital projects)", f"{r[1]}/{r[0]} names matched",
          f"{r[2]} station rows; {r[3]} of them already show as accessible in the station list and are dropped", r[1] / r[0] >= 0.85)
    con.sql("DELETE FROM station_upgrades WHERE ada_today = 1")
    con.sql("""
        CREATE OR REPLACE TABLE zone_upgrades AS
        SELECT z.zone_id, count(*) AS upgrade_stations,
               string_agg(u.stop_name || ' (' || u.routes || ')' || coalesce(' — MTA forecast ' || strftime(u.forecast, '%b %Y'), ' — in procurement, no date'),
                          '; ' ORDER BY u.forecast NULLS LAST, u.stop_name) AS upgrade_names,
               min(u.forecast) AS upgrade_earliest
        FROM zones z JOIN zone_home h USING (zone_id) JOIN station_upgrades u
          ON ST_Contains(z.geom_ft, u.pt_ft) OR ST_DWithin(h.home_ft, u.pt_ft, 800 / 0.3048)
        GROUP BY z.zone_id
    """)
    con.sql("ALTER TABLE station_upgrades DROP COLUMN pt_ft")
    r = con.sql("""SELECT count(*), count(*) FILTER (zone_id IN (SELECT zone_id FROM zone_features WHERE ada_stations_800m = 0 AND mf_units >= 1000))
                   FROM zone_upgrades""").fetchall()[0]
    check("zones with a station due to become accessible", r[0], f"{r[1]} of them have no accessible station today")

    # ---- elevator outages right now + planned (snapshot at fetch time)
    eq, out = json.load(open(f"{X}/nyct_ene_equipments.json")), json.load(open(f"{X}/nyct_ene.json"))
    fetched = dt.datetime.fromtimestamp(os.path.getmtime(f"{X}/nyct_ene.json"))
    by_eq = {e["equipmentno"]: e for e in eq}
    fmt = "%m/%d/%Y %I:%M:%S %p"
    rows, unmatched = [], 0
    for o in out:
        if o.get("equipmenttype") != "EL" or o.get("ADA") != "Y":
            continue
        e = by_eq.get(o["equipment"])
        if not e or not str(e.get("stationcomplexid", "")).isdigit():
            unmatched += 1
            continue
        try:
            start, end = dt.datetime.strptime(o["outagedate"], fmt), dt.datetime.strptime(o["estimatedreturntoservice"], fmt)
        except Exception:
            unmatched += 1
            continue
        rows.append((int(e["stationcomplexid"]), o["equipment"], o["station"], o["serving"], o["reason"],
                     o["isupcomingoutage"] == "Y", start, end, round((end - start).total_seconds() / 86400, 1)))
    con.execute("""CREATE OR REPLACE TABLE elevator_outages (complex_id INT, equipment VARCHAR, station VARCHAR, serving VARCHAR,
                   reason VARCHAR, upcoming BOOLEAN, starts TIMESTAMP, returns TIMESTAMP, days DOUBLE)""")
    if rows:
        con.executemany("INSERT INTO elevator_outages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE OR REPLACE TABLE elevator_redundancy (complex_id INT, ada_elevators INT, no_backup INT)")
    red: dict[int, list[int]] = {}
    for e in eq:
        if e.get("equipmenttype") == "EL" and e.get("ADA") == "Y" and e.get("isactive") == "Y" and str(e.get("stationcomplexid", "")).isdigit():
            a = red.setdefault(int(e["stationcomplexid"]), [0, 0])
            a[0] += 1
            a[1] += 1 if e.get("redundant") in (0, "0") else 0
    con.executemany("INSERT INTO elevator_redundancy VALUES (?, ?, ?)", [(k, v[0], v[1]) for k, v in red.items()])
    con.execute("CREATE OR REPLACE TABLE feed_info AS SELECT ?::TIMESTAMP AS elevator_feed_fetched", [fetched])
    r = con.sql("""SELECT count(*) FILTER (NOT upcoming), count(*) FILTER (NOT upcoming AND days >= 7), count(*) FILTER (upcoming),
                          max(returns) FILTER (upcoming) FROM elevator_outages""").fetchall()[0]
    check(f"MTA elevator status feed (snapshot {fetched:%d %b %H:%M})", f"{r[0]} accessible elevators out now",
          f"{r[1]} of them for a week or longer; {r[2]} planned outages listed through {r[3]:%d %b}; {unmatched} rows could not be matched to a station")
    r = con.sql("SELECT count(*), count(*) FILTER (no_backup > 0), (SELECT count(*) FROM elevator_redundancy WHERE complex_id IN (SELECT complex_id FROM stations)) FROM elevator_redundancy").fetchall()[0]
    check("JOIN elevator equipment list → station complexes", f"{r[2]}/{r[0]}", f"{r[1]} complexes have at least one elevator with no backup")
