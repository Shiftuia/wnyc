You are building, tonight, a working app for the Fable 5.2 Build Day. Topic: accessibility. Lane: Smart — "An accessibility tool that wasn't possible before." Read ./research/*.md first: they contain verified dataset URLs, column names, measured numbers and known gotchas. Trust them over your memory; if something there turns out wrong when you load the data, say so and adapt.

GOAL
A local web app where a person types: "I use a wheelchair, my budget is $3,500/month, I work at <any NYC address>" and gets, live:
1) top-3 NYC neighbourhoods (taxi zones) where they could realistically live, each with: score, typical rent, nearest accessible subway station and its elevator uptime, whether there is a one-seat accessible ride to work, typical wheelchair-accessible Uber/Lyft wait, the 3 biggest pluses and the 1 biggest risk;
2) a ranked accessible table of all qualifying zones (primary result) and a choropleth map (secondary);
3) an "accessibility deserts" view: zones that are affordable but have no accessible station within 800 m and slow WAV pickup;
4) a "How I decided" panel with the SQL you ran and the data checks.

DATA (details in research/01): run scripts/fetch.sh if ./data is empty. HVFHV Sept 2026 (WAV waits — completed trips only, never claim a "match rate"), taxi zones (EPSG:2263 → transform to 4326), MTA stations with ADA, MTA elevator availability (Apr–Oct 2026), PLUTO multifamily lots (1991 Fair Housing Act proxy + elevator buildings), Zillow ZORI by ZIP (ZIP→zone via PLUTO lots weighted by units), curb ramps (optional).

BUILD ORDER (research/05; stop and show me after each step):
1. Data layer in DuckDB (spatial + httpfs): build `zone_features` per research/02, cache to data/zone_features.parquet. Print row counts per source, join match rates (elevator→station, lots→zones, ZIP→rent), and 5 sample zones. Do not impute silently; nulls are shown as "no data".
2. Agent: Claude API (`anthropic` SDK), model "claude-fable-5-2", effort "medium", no `thinking` param, tool_choice auto (forced tool use and manual thinking budgets return 400 on this model; keep history append-only). Tools: run_sql (read-only over prepared tables), geocode (Nominatim with User-Agent, fallback to fixed work hubs), render_map (returns PNG). The model parses the free-text question into budget + work location + needs, queries, scores per research/02 (weights visible), then calls render_map and LOOKS at the image to check the map agrees with its top-3, and says what it checked.
3. UI per research/03 in Streamlit: one text box + 3 example chips, answer card, accessible ranked table, map, deserts toggle, sources + caveats footer. The app must itself be accessible: keyboard usable, high contrast, colour-blind-safe palette, no colour-only meaning, table as primary result.
4. Rehearse: run the demo question "I use a wheelchair, my budget is $3,500, I work at 350 5th Ave, Manhattan" twice; confirm the same top-3; then "work at Bronx Hub". Fix anything slow (>20 s) by precomputing.

RULES
- Keep the context small: never print raw data files; print aggregates, counts and samples (<=10 rows). After each build step, write a short PROGRESS.md (what works, how to run, next step) so a fresh session can continue.
- Before any big change copy the working app folder to a backup.
- Honest numbers only: every figure in the answer comes from a query you ran tonight. Proxies are labelled as proxies (FHA 1991 rule ≠ verified accessible unit; ZORI = typical rent).
- Prefer working over complete: follow the cut list in research/05 if behind schedule. Never cut rent + ADA stations + agent answer + accessible table.
- Keep it simple: Python, DuckDB, Streamlit, one repo folder. No Spark, no database server, no auth.
- At the end print: how to run it, the exact demo question, and a 3-line summary of what the model did (for the "show your prompt" moment).
