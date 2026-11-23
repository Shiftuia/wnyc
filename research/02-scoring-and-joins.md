# 02 — Score and joins

Unit: taxi zone (263). Everything is aggregated to a zone table `zone_features`, computed once at startup and cached as parquet.

## Columns of zone_features
- rent_zori: unit-weighted ZORI of the zone (USD/month), null if no coverage
- fha_units_share: units in buildings yearbuilt>=1991 & unitsres>=4 / all units in buildings unitsres>=4
- elevator_units_share: units in bldgclass D* (elevator apartments) / all multifamily units
- ada_stations_800m: count of ada IN (1,2) stations within 800 m of the zone centroid (or inside the zone + buffer); list their names and routes
- ada_elevator_uptime: mean 24h elevator availability (Apr–Oct 2026) over those stations; null if no match
- wav_wait_median_min, wav_wait_p90_min, wav_trips: from HVFHV, pickup zone, wav_request='Y', wait in 0..3600 s
- ramp_good_share: ramps with dws_conditions='Good Condition' / ramps in zone

## Hard filters (from the question)
1. rent_zori <= budget (null → shown grey "no rent data", not excluded silently)
2. at least one ADA station within 800 m of home AND of work (work = geocoded point). If none at work: say so and rank by plan-B (bus + WAV) instead.

## Commute check (simple, honest)
- Accessible line connection: home ADA station and work ADA station share at least one route in daytime_routes → "one-seat accessible ride". Else "needs a transfer — check that transfer station is accessible" (look it up: any ADA station serving both routes).
- No door-to-door routing. Say so.

## Score (0–100), explainable, weights visible in the UI
- 30 housing: fha_units_share and elevator_units_share (scaled to city percentiles)
- 30 transit: one-seat accessible ride (20) + elevator uptime of home station (10)
- 15 street: ramp_good_share
- 15 plan B: WAV wait (lower = better, city percentiles)
- 10 rent headroom: (budget − rent) / budget
Every recommendation prints its 3 biggest pluses and 1 biggest risk ("elevator at X was up only 74% of the time").

## Sanity checks the agent must run and show (judges: "It works")
- counts per source loaded; join match rates (elevator→station, lots→zones, ZIP→rent)
- no zone with WAV wait computed from < 30 trips (mark "too few rides")
- Top-3 for the demo question re-computed twice gives the same answer
