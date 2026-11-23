# 03 — Interface

## Principle: the accessibility app must itself be accessible
Judges will notice if a wheelchair-accessibility tool is a mouse-only map. Do these, they are cheap:
- Every map result is also a plain ranked list/table (screen readers cannot read maps). The list is the primary result; the map is secondary.
- Full keyboard use: tab order input → results → map; visible focus outline.
- High contrast, colour-blind-safe palette (viridis), never colour-only meaning: each zone label also has its score number.
- Big hit targets, readable font (>=16 px), `lang="en"`, alt text on the map image, aria-live region for the answer.
- Optional if time: voice input (browser Web Speech API) — a nice live moment, but cut first.

## Layout (one page)
- Top: one text box: "Describe your situation" with placeholder "I use a wheelchair, budget $3,500, I work at 350 5th Ave". Submit button. Three example chips (Midtown / Downtown Brooklyn / Bronx Hub).
- Left (55%): answer card from the model: Top-3 neighbourhoods, each with score, rent, nearest accessible station + its elevator uptime, one-seat ride yes/no, WAV wait, the biggest risk. Then "How I decided" (expandable): the SQL it ran and the checks.
- Right (45%): choropleth of all zones by score; work location pin; accessible stations as dots (green >95% uptime, amber 85–95, red <85); top-3 outlined.
- Footer: data sources + "proxies, not guarantees" caveats.
- Hero toggle for the demo: "Accessibility deserts" — zones that are cheap but have no accessible station within 800 m and a long WAV wait.

## Stack (fastest that works in 90 min)
- Python + DuckDB (spatial + httpfs) for data, Streamlit for UI (st.chat_input / text_input, st.dataframe for the accessible table, streamlit-folium or pydeck for the map).
- Claude API via `anthropic` SDK, model `claude-fable-5-2`, tools: `run_sql` (read-only DuckDB over the prepared tables), `geocode`, `render_map` (returns PNG for the self-check).
- If Streamlit's accessibility is a concern and time allows: FastAPI + one static HTML + Leaflet. Default: Streamlit.
