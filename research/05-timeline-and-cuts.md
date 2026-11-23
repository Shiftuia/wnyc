# 05 — 90-minute plan, cut list, fallbacks

- 0–10: fetch.sh running; register the project in the form; Claude Code on the Org key; paste MAIN_PROMPT.
- 10–35: data layer: zone_features built and validated (counts, match rates printed).
- 35–60: agent loop (run_sql, geocode, render_map) + answer card.
- 60–75: map + accessible table + deserts toggle + self-check turn.
- 75–85: rehearse the exact demo question twice; cache it.
- 85–90: freeze. No new features.

Cut order if late (cut from the top):
1. voice input
2. ramps / 311 / ramp complaints
3. transfer-station logic (keep one-seat yes/no)
4. elevator uptime (keep ADA yes/no)
5. WAV layer
Never cut: rent + ADA stations + agent answer + accessible ranked list.

Fallbacks
- HVFHV download slow → query it remotely via DuckDB httpfs (aggregates take ~2 s).
- PLUTO API slow → drop housing proxy, keep rent; say so.
- Nominatim blocked → fixed work-hub dropdown.
- Elevator join < 70% match → show uptime only where matched, label the rest "no elevator data".
