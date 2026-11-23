# 01 — Data sources (all checked live 2026-11-23 ~17:40 IST)

## A. Rides: NYC TLC High-Volume FHV trips (Uber/Lyft) — taxi layer
- URL: https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2026-09.parquet (510 MB, 20,987,681 rows). 2026-08 and 2026-07 also exist.
- Confirmed columns: request_datetime, on_scene_datetime, pickup_datetime, dropoff_datetime, PULocationID, DOLocationID, trip_miles, trip_time, base_passenger_fare, ..., shared_request_flag, shared_match_flag, **access_a_ride_flag, wav_request_flag, wav_match_flag** (Y/N strings).
- DuckDB reads it remotely in ~2 s per aggregate (httpfs) — you may not even need the download.
- **GOTCHA 1 (important): the file has completed trips only.** In Sept 2026 there are 0 rows with wav_request=Y and wav_match=N. So "share of WAV requests that found a car" is NOT computable — unfulfilled requests are not in the data. Do not claim it.
- What IS computable and honest:
  - WAV wait = on_scene_datetime − request_datetime for wav_request_flag='Y', per pickup zone (median, p90). Filter waits to 0..3600 s (negative values exist = pre-booked rides).
  - WAV trips per 1,000 trips per zone (how common accessible rides are there).
  - access_a_ride_flag='Y' trips per zone (paratransit rides operated through Uber/Lyft) — a proxy for where disabled riders live/travel.
- Measured (Sept 2026, wav_request=Y): 68,955 trips. Borough median wait: Bronx 4.0 min, Manhattan 4.0, Staten Island 3.9, Brooklyn 3.6, Queens 3.5. Zone medians range ~1 to 6.6 min (JFK 6.6, South Williamsburg 6.0, Riverdale 5.3). All riders median ~3.6 min (214 s) vs WAV 3.8 min (228 s).
- Conclusion: taxi is a SUPPORTING layer ("plan B when the elevator is broken"), not the hero. The spread is modest; present it honestly.

## B. Taxi zones (the spatial unit for everything)
- https://d37ci6vzurychx.cloudfront.net/misc/taxi_zones.zip (shapefile, 263 zones) + taxi_zone_lookup.csv (LocationID, Borough, Zone, service_zone).
- **GOTCHA 2: the shapefile is in NY State Plane (EPSG:2263, feet), not lat/lon.** Transform to EPSG:4326 before joining lat/lon points or drawing on a web map. DuckDB: `INSTALL spatial; LOAD spatial; ST_Read('/vsizip/data/taxi_zones.zip/taxi_zones.shp')` then `ST_Transform(geom, 'EPSG:2263', 'EPSG:4326', always_xy := true)`; or geopandas `.to_crs(4326)`.

## C. Subway stations with ADA status — the hero layer
- data.ny.gov dataset 39hk-dx4f "MTA Subway Stations" (CSV/JSON API). Fields: station_id, complex_id, stop_name, borough (M/Bk/Q/Bx/SI), daytime_routes, gtfs_latitude, gtfs_longitude, **ada** (0 = no, 1 = fully accessible, 2 = partially), ada_northbound, ada_southbound, ada_notes.
- Measured: 496 station rows; ada=1: 166, ada=2: 8, ada=0: 322 → only about 1 in 3 stations is accessible. Manhattan 65/153 full, Brooklyn 44/169, Queens 28/83, Bronx 23/70, SI 6/21.

## D. Elevator availability (is the "accessible" station really usable?)
- data.ny.gov rc78-7x78 "MTA NYCT Subway Elevator and Escalator Availability: Beginning 2015", monthly per device, latest month 2026-10. Fields: month, borough, equipment_type (Elevator/Escalator), equipment_code, total_outages, unscheduled_outages, entrapments, am_peak_availability, pm_peak_availability, _24_hour_availability, station_name, station_mrn, station_complex_name, station_complex_mrn.
- Measured worst elevator complexes Apr–Oct 2026 (avg 24h availability): 161 St-Yankee Stadium 0.56, 3 Av-149 St 0.74, Times Sq-42 St 0.89, Lexington Av-53 St 0.89.
- **GOTCHA 3: join to stations.** Try station_complex_mrn = complex_id first and check match rate; fallback = normalized name match on station_complex_name vs stop_name. Report unmatched count; do not silently drop.
- Also: thh2-syn7 "Subway Accessible Station Platform Availability" (borough-month level, coarse) — skip unless needed. 6yjv-fk7g "Subway Station Amenities" (has lat/lon per amenity) — optional.

## E. Housing that can be accessible — PLUTO + the 1991 rule
- NYC Open Data 64uk-42ks (PLUTO). Fields used: bbl, zipcode, yearbuilt, numfloors, unitsres, latitude, longitude, bldgclass.
- The fact behind the metric: the US Fair Housing Act design requirements apply to multifamily buildings with 4+ units first occupied after 13 March 1991 (all ground-floor units, and all units in elevator buildings, must be accessible/adaptable). Proxy per zone: share of residential units in buildings with yearbuilt >= 1991 and unitsres >= 4, and separately in elevator-likely buildings (numfloors >= 6, or bldgclass D* = elevator apartments — D is "elevator apartments" in NYC building classes, C is walk-ups).
- This is a proxy, not a list of accessible units. Say so in the UI.

## F. Rent
- Zillow ZORI by ZIP: https://files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv (10 MB, wide: one column per month, rows = ZIPs; RegionName = ZIP, City, Metro). Use the latest non-null month. Filter to NYC ZIPs (City == 'New York' or ZIP present in PLUTO).
- ZIP → taxi zone: use PLUTO lots (they have zipcode + lat/lon): spatial-join lots to zones, weight by unitsres → each zone gets a unit-weighted average rent. Zones with no ZORI coverage = "no rent data" (grey), never imputed silently.
- ZORI is a typical market rent across all sizes; $3,500 means "typical unit is within budget". Caveat in UI.

## G. Street level — curb ramps and complaints
- ufzp-rrqu "Pedestrian Ramp Locations" (DOT survey, ~2019–2021, point per ramp with condition fields: dws_conditions, ramp_running_slope_total, ponding, obstacles_ramp ...). Per zone: ramps in "Good Condition" share.
- jagj-gttd "Pedestrian Ramp Complaints" (updated 2026-11): borough, community_district, block, lot, complaint_date — no lat/lon; join via BBL→PLUTO lat/lon if time allows, else community district level.
- 311 (erm2-nwe9) "Sidewalk Condition" has lat/lon but the API is slow (60 s timeouts seen); optional.

## H. Geocoding the workplace
- Free, no key: Nominatim (OSM) `https://nominatim.openstreetmap.org/search?q=<address>&format=json&limit=1` with a User-Agent header; 1 req/s. Then point-in-polygon → taxi zone.
- Offline fallback: a dropdown of ~10 common work hubs (Midtown Center, Times Sq, FiDi, Downtown Brooklyn, LIC, Hudson Yards, Columbia/Morningside, Jamaica, Bronx Hub) with fixed coordinates.

## Not used, on purpose
- WiGLE Wi-Fi: rate-limited API, bulk data needs approval, privacy question. Out.
- Yellow taxi (not FHV): no WAV fields in the current yellow schema; HVFHV is the right file.
- Spark: DuckDB does every aggregate here in seconds on a laptop.
