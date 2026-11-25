# PROGRESS — Wheelchair NYC (last updated 2026-11-23, evening)

## What works
1. **Data layer** — `app/build_data.py` (+ `app/build_extra.py`) → `data/prepared.duckdb`, ~20 s (first run ~3 min: it
   samples elevations politely). Row counts and join match rates are printed, stored in `data_checks`, shown in the UI.
   Core: rents, ADA stations, elevator uptime, WAV waits, PLUTO, ramps. Enrichment (`scripts/fetch_extra.sh` →
   `data/extra/`): residential elevator complaints, housing lotteries, bus stops, pedestrian injuries, accessible
   restrooms, APS, hospitals/clinics, CDC mobility prevalence, hilliness, reported felonies (opt-in), live elevator
   outages, stations due to become accessible, zone→zone Uber/Lyft fares and times by time of day, subway timetable.
   None of the enrichment changes the 0–100 score.
2. **Agent** — `app/agent.py`: `claude-fable-5-2`, effort medium, no `thinking` param, tool_choice auto, append-only
   history, server-side refusal fallback. Tools: `geocode`, `score_zones`, `run_sql`, `plan_trip`, `render_map` (PNG
   the model looks at). Falls back to a labelled rule-based path with no credential or on API errors. Runs are cached
   per question in `data/cache/` (delete `run_*.json`, or Shift+submit, to force a fresh run).
3. **Trip check** — `app/router.py`: A → B by wheels, accessible subway (timetable graph, accessible stations only),
   accessible Uber/Lyft (real trips for the chosen time of day), or a mix; minutes, cost, effort, "alone / with a
   risk / help likely", live elevator outages. Profiles: manual chair, power chair, cane or walker.
4. **UI** — two pages, hand-written, no framework:
   - `/` short landing (hook, the same-rent pair, three numbers, one button);
   - `/map` the app: full-screen street map (MapLibre + OpenFreeMap, keyless) with a side panel — Ask, Results,
     Trip, Layers, About. Standard colours by default; "Accessible colours" switches to a colour-blind-safe palette
     with patterns and numbers. 3D tilt, budget dial, deserts switch, layer picker, crime as an opt-in layer.
5. **Rehearsed** — "…$3,500, I work at 350 5th Ave, Manhattan": Madison, Sheepshead Bay, Coney Island (same on
   repeat). Ranking shows ~3 s after submit; the written answer takes 27–75 s live (longer when the model also
   checks the trip), instant when replayed from cache.

## Run
`./run.sh` → http://127.0.0.1:8765  (`ANTHROPIC_API_KEY` in the environment or `.env` enables the model path).
A frozen copy for demos can be served from `stable/` with `PORT=8766 .venv/bin/python stable/app/server.py`.
Deep links: `/map?q=<question>` plus `&layer=elev|bus|hill|…`, `&view=deserts`, `&3d=1`, `&a11y=1`, `&zone=<id>`,
`&trip=<from>|<to>`.

## Where the data disagreed with research/*.md
- `data/ramps.csv` from fetch.sh was truncated (147,776 of 217,679 rows). Re-fetched in two pages → `ramps_0.csv`,
  `ramps_110000.csv` (the truncated file is kept as `ramps.truncated.csv`; fetch.sh not yet fixed).
- WAV median pickup wait is 4.97 min (not 3.8) vs 3.62 for other riders; borough medians 4.8–6.4 min.
- 161 St-Yankee Stadium elevator availability is 0.66 by mean-of-elevators (notes said 0.56).
- taxi_zones.zip has the shapefile in a `taxi_zones/` subfolder.

## Decisions that are mine (change if you disagree)
- Zone "reach" = ADA stations inside the zone or within 800 m of the unit-weighted centre of its housing.
- Zones with < 1,000 multifamily units are not ranked. Missing data scores 0 for that component and is labelled.
- Elevator-uptime points: 80% → 0, 100% → 10. Rent headroom capped at 40%. Transfer commute = 10 of 20 points.
- Desert = affordable, no ADA station in reach, WAV median wait ≥ city zone median (5.0 min) or unmeasurable.
- Plan-B fare = median Uber/Lyft fare home zone → work zone over all trips (WAV and standard price alike per mile).

## Next
- Not done from the research: street-closure and parks-construction warnings, planned service-change alerts.
- `station_upgrades` is a hand-typed list from MTA capital project descriptions (app/build_extra.py: UPGRADES).
- The map needs internet for street tiles; offline it falls back to zone shapes on a plain background.
- Bus trips are not routed (no travel-time data); "alone / help" labels are heuristics.
- Fix scripts/fetch.sh ramps download (page it). Travel time by subway is not modelled (straight-line km + car minutes only).
- No automated tests yet; keyboard and screen-reader behaviour was designed for but not tested with a real screen reader.
