"""Step 2 — the agent.

Claude (claude-haiku-5-5 by default, set WNYC_MODEL to change; effort medium) parses the free-text question, geocodes the workplace, scores the
zones, runs its own verification SQL, renders the map and LOOKS at the PNG to check it agrees with the top-3.

`run(question)` is a generator of UI events. Without an API credential it falls back to a rule-based
pipeline over the same deterministic engine (clearly labelled in the UI; the visual self-check is skipped).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator

import engine
import router

MODEL = os.environ.get("WNYC_MODEL", "claude-haiku-5-5")   # e.g. WNYC_MODEL=claude-fable-5-2
EFFORT = os.environ.get("WNYC_EFFORT", "medium")
# server-side refusal fallback exists for Fable / Opus 5.x / Sonnet 5.5, not for Haiku
_FALLBACK = {} if "haiku" in MODEL else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
MAX_TURNS = 12
CACHE_DIR = engine.DATA / "cache"
MAPS: dict[str, bytes] = {}  # map id → PNG bytes (served by the web server)


def _load_dotenv() -> None:
    p = engine.ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


_load_dotenv()


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")
                or (Path.home() / ".config" / "anthropic").exists())


SYSTEM = f"""You are the analyst inside "Wheelchair NYC", a tool that helps a wheelchair user decide where in \
New York City they could realistically live. The person types one free-text message with (usually) a monthly \
rent budget and a workplace. The interface around you already renders the top-3 cards, the full ranked table, \
the map and the "accessibility deserts" view from the output of your score_zones call, and shows every tool \
call you make in a "How I decided" panel. Your written answer sits above all that, so it should add judgement, \
not repeat the table.

How to work:
- Work out the budget (USD per month) and the workplace from the message. If either is truly missing, do not \
guess: reply with one short, friendly question and stop. Mention any other need they state (e.g. power chair, \
needs a car service) and take it into account in your advice.
- geocode the workplace, then call score_zones. It applies the documented scoring (weights are returned with \
the result) and is deterministic.
- Keep yourself honest with run_sql: independently verify at least two facts your answer relies on (for \
example the top zone's rent and its home station's elevator record month by month, or how many zones pass each \
filter). If a check disagrees with score_zones, say so plainly.
- Each zone in the result carries "alerts": dated things about to change (an elevator out right now, planned \
outages, a station due to become accessible, an open affordable-housing lottery). Mention one when it changes the \
advice. You may call plan_trip once to check the commute from your first choice (origin "lat,lon" of its home \
station is fine) when distance or a transfer makes the commute the deciding factor.
- Call render_map and actually look at the image. Check that the three outlined, numbered zones are the ones \
you are recommending, that the workplace star is where you expect, that the station markers near the top zones \
match the uptime figures you quote, and flag anything that looks odd (a top zone far from any station marker, \
a surprising cluster, a zone coloured unlike its neighbours).

Your final answer (plain language, a reader may be using a screen reader; at most ~170 words; no tables; no headings other than the two bold labels below):
1. Two or three sentences: which of the top-3 you would pick first and why, and the real trade-off between \
them (distance to work, a single elevator as the point of failure, slow plan-B rides, rent headroom).
2. A paragraph starting with **Map check:** saying what you looked at in the image and whether it agreed.
3. A paragraph starting with **Keep in mind:** with the one caveat that matters most for this person.

Honesty rules — these matter more than sounding helpful:
- Every number you state must come from a tool result in this conversation.
- The ride-hail file contains completed trips only: never state or imply a share of wheelchair-accessible \
requests that were fulfilled.
- "Post-1991 building" and "elevator building" are proxies for accessible housing, not a list of accessible \
units. Rent is Zillow's typical market rent for the area, not the rent of an accessible apartment.
- There is no door-to-door routing: "one-seat accessible ride" means an accessible home station and an \
accessible work station share a subway route.
- Missing values are "no data"; never fill them in.

{engine.SCHEMA_DOC}"""

TOOLS = [
    {"name": "geocode",
     "description": "Turn a NYC street address or place name into latitude/longitude and its taxi zone. Uses "
                    "OpenStreetMap Nominatim, falling back to a built-in list of common work hubs. Call this "
                    "before score_zones.",
     "input_schema": {"type": "object", "properties": {"address": {"type": "string"}},
                      "required": ["address"], "additionalProperties": False},
     "eager_input_streaming": True},
    {"name": "score_zones",
     "description": "Rank all 263 taxi zones for a wheelchair user with this monthly rent budget and workplace. "
                    "Applies the hard filters (rent within budget, an ADA station in reach of home) and the "
                    "0–100 score, and returns the weights, filter counts, the top zones with components, "
                    "pluses and biggest risk, and the accessibility deserts. The UI renders the full result.",
     "input_schema": {"type": "object",
                      "properties": {"budget": {"type": "number", "description": "USD per month"},
                                     "work_lat": {"type": "number"}, "work_lon": {"type": "number"},
                                     "work_label": {"type": "string", "description": "short human label for the workplace"}},
                      "required": ["budget", "work_lat", "work_lon", "work_label"], "additionalProperties": False},
     "eager_input_streaming": True},
    {"name": "run_sql",
     "description": "Run one read-only DuckDB SELECT over the prepared tables (schema in the system prompt). "
                    "Returns at most 50 rows — aggregate, do not dump. Use it to verify facts independently.",
     "input_schema": {"type": "object",
                      "properties": {"sql": {"type": "string"},
                                     "purpose": {"type": "string", "description": "one line: what this checks"}},
                      "required": ["sql", "purpose"], "additionalProperties": False},
     "eager_input_streaming": True},
    {"name": "render_map",
     "description": "Render the latest score_zones result as a PNG (city view + zoom on the top-3) and return "
                    "the image so you can inspect it. view='score' colours qualifying zones by score; "
                    "view='deserts' highlights affordable zones with no accessible station and slow ride-hail.",
     "input_schema": {"type": "object",
                      "properties": {"view": {"type": "string", "enum": ["score", "deserts"]}},
                      "required": ["view"], "additionalProperties": False},
     "eager_input_streaming": True},
]
TOOLS.append(
    {"name": "plan_trip",
     "description": "Check one A → B trip for a wheelchair user: every way to make it (wheel, accessible subway, accessible "
                    "Uber/Lyft, or a mix), each with minutes, cost, pushing distance and whether it can be done alone. Uses "
                    "the subway timetable restricted to accessible stations, live elevator outages and ride-hail trips for "
                    "the chosen time of day. Use it when the person asks how they would get somewhere, or to sanity-check "
                    "the commute from your first-choice zone (origin can be 'lat,lon').",
     "input_schema": {"type": "object",
                      "properties": {"origin": {"type": "string", "description": "address, place name, or 'lat,lon'"},
                                     "destination": {"type": "string"},
                                     "when": {"type": "string", "enum": ["am_peak", "midday", "pm_peak", "night", "weekend"]},
                                     "chair": {"type": "string", "enum": ["manual", "power", "walker"], "description": "manual or power wheelchair, or walker = walks with a cane or walker"}},
                      "required": ["origin", "destination", "when", "chair"], "additionalProperties": False},
     "eager_input_streaming": True})
_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}
_TYPES = {"string": str, "number": (int, float)}


def _validate(name: str, inp) -> str | None:
    """Eager input streaming skips server-side validation, so check the parsed input ourselves."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        return f"unknown tool {name}"
    if not isinstance(inp, dict):
        return "input must be an object"
    for k in schema["required"]:
        if k not in inp:
            return f"missing required field '{k}'"
    for k, v in inp.items():
        spec = schema["properties"].get(k)
        if spec is None:
            return f"unexpected field '{k}'"
        if not isinstance(v, _TYPES[spec["type"]]) or isinstance(v, bool):
            return f"field '{k}' must be a {spec['type']}"
        if "enum" in spec and v not in spec["enum"]:
            return f"field '{k}' must be one of {spec['enum']}"
    return None


def _store_map(res: dict, view: str) -> tuple[str, bytes]:
    png = engine.render_map_png(res, view)
    mid = hashlib.sha1(png).hexdigest()[:16]
    MAPS[mid] = png
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / f"map_{mid}.png").write_bytes(png)
    return mid, png


def load_map(mid: str) -> bytes | None:
    if mid in MAPS:
        return MAPS[mid]
    p = CACHE_DIR / f"map_{mid}.png"
    return p.read_bytes() if re.fullmatch(r"[0-9a-f]{16}", mid) and p.exists() else None


def _repro_check(res: dict) -> dict:
    p = res["params"]
    again = engine.score(p["budget"], p["work_lat"], p["work_lon"], p["work_label"])
    a, b = [r["zone_id"] for r in res["top3"]], [r["zone_id"] for r in again["top3"]]
    return {"type": "check", "name": "Top-3 recomputed a second time", "ok": a == b,
            "detail": "same three zones in the same order" if a == b else f"MISMATCH: {a} vs {b}"}


# ------------------------------------------------------------------ fable mode
def _run_fable(question: str) -> Iterator[dict]:
    import anthropic

    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": question}]  # append-only history
    state: dict = {"result": None}
    n_sql = 0

    for turn in range(MAX_TURNS):
        yield {"type": "step", "label": "Thinking" if turn == 0 else "Reading the results",
               "detail": f"{MODEL} · effort {EFFORT}", "status": "active", "kind": "model"}
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOLS,                               # tool_choice stays "auto" (forced tool use → 400)
                output_config={"effort": EFFORT},          # no `thinking` param: adaptive thinking is always on
                **_FALLBACK,                               # a safety-classifier decline is retried on a fallback model
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield {"type": "answer_delta", "text": text}
                response = stream.get_final_message()
        except ValueError as e:  # eager-streamed tool input that could not be parsed at all
            raise RuntimeError(f"malformed tool input from the model: {e}") from e

        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("the model ran out of output tokens")
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            yield {"type": "answer", "text": text, "model": response.model}
            return
        yield {"type": "answer_reset"}  # text before a tool call was a working note, not the answer
        note = "".join(b.text for b in response.content if b.type == "text").strip()
        if note:
            yield {"type": "step", "label": "Note", "detail": note[:400], "status": "done", "kind": "note"}

        results = []
        for tu in tool_uses:
            err = _validate(tu.name, tu.input)
            if err:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"INVALID_INPUT: {err}", "is_error": True})
                continue
            inp = tu.input
            content: str | list
            if tu.name == "geocode":
                out = engine.geocode(inp["address"])
                yield {"type": "step", "label": "Located the workplace" if "error" not in out else "Could not locate the workplace",
                       "detail": (f"{inp['address']} → {out['zone_name']}, {out['borough']} ({out['source']})"
                                  if "error" not in out else out["error"]),
                       "status": "done" if "error" not in out else "warn", "kind": "geocode"}
                content = json.dumps(out)
            elif tu.name == "score_zones":
                if not (40.4 < inp["work_lat"] < 41.0 and -74.3 < inp["work_lon"] < -73.6) or not (300 <= inp["budget"] <= 50000):
                    content = json.dumps({"error": "workplace must be inside NYC and budget between $300 and $50,000"})
                else:
                    res = engine.score(inp["budget"], inp["work_lat"], inp["work_lon"], inp["work_label"][:80])
                    state["result"] = res
                    c = res["counts"]
                    yield {"type": "step", "label": "Scored all 263 zones",
                           "detail": f"{c['affordable']} within budget → {c['qualifying']} also have an accessible station; {c['deserts']} deserts",
                           "status": "done", "kind": "score"}
                    yield {"type": "result", "data": res}
                    yield _repro_check(res)
                    content = json.dumps(engine.compact_for_model(res))
            elif tu.name == "run_sql":
                out = engine.run_sql(inp["sql"])
                n_sql += 1
                yield {"type": "sql", "purpose": inp["purpose"][:200], "sql": inp["sql"], "by": "model",
                       "columns": out.get("columns"), "rows": (out.get("rows") or [])[:10],
                       "row_count": out.get("row_count"), "error": out.get("error")}
                content = json.dumps(out)
            elif tu.name == "plan_trip":
                def _pt(v: str):
                    m = re.fullmatch(r"\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*", v)
                    return router.locate({"lat": float(m.group(1)), "lon": float(m.group(2))} if m else v)
                a, b = _pt(inp["origin"]), _pt(inp["destination"])
                if "error" in a or "error" in b:
                    content = json.dumps({"error": a.get("error") or b.get("error")})
                else:
                    tr = router.plan(a, b, inp["when"], inp["chair"])
                    best = tr["options"][0] if tr["options"] and tr["options"][0]["possible"] else None
                    yield {"type": "step", "label": "Checked the trip door to door", "kind": "trip", "status": "done",
                           "detail": f"{a['label']} → {b['label']}: " + (f"{best['title'].lower()}, about {best['minutes']} min" if best else "no workable option")}
                    yield {"type": "trip", "data": router.add_paths(tr)}
                    content = json.dumps({"from": a, "to": b, "when": tr["when_label"], "assumptions": tr["assumptions"],
                                          "options": [{k: o.get(k) for k in ("title", "possible", "why_not", "minutes", "cost", "cost_reduced", "push_m",
                                                                             "transfers", "elevator_reliability", "independence", "best_for")}
                                                      | {"steps": [{k: v for k, v in s.items() if k not in ("from_pt", "to_pt", "path")} for s in o["steps"]]}
                                                      for o in tr["options"]]}, default=str)
            else:  # render_map
                if state["result"] is None:
                    content = json.dumps({"error": "call score_zones first"})
                else:
                    mid, png = _store_map(state["result"], inp["view"])
                    yield {"type": "step", "label": "Rendered the map and is looking at it",
                           "detail": f"{len(png) // 1024} KB PNG sent back to the model for a visual self-check",
                           "status": "done", "kind": "map"}
                    yield {"type": "map", "id": mid, "view": inp["view"], "seen_by_model": True}
                    content = [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.standard_b64encode(png).decode()}},
                        {"type": "text", "text": "Rendered map of the latest score_zones result (left: whole city, "
                                                 "right: zoom on the top-3). Inspect it before answering."},
                    ]
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
        messages.append({"role": "user", "content": results})  # all results in one user message
    raise RuntimeError("the agent did not finish within the turn limit")


# ---------------------------------------------------------------- offline mode
def _run_offline(question: str, reason: str) -> Iterator[dict]:
    yield {"type": "step", "label": "Read the question with fixed rules", "kind": "model",
           "detail": reason, "status": "warn"}
    p = engine.parse_question(question)
    if not p["budget"] or not p["work"]:
        missing = " and ".join(x for x, ok in (("your monthly rent budget", p["budget"]), ("where you work", p["work"])) if not ok)
        yield {"type": "answer", "model": None,
               "text": f"I could not find {missing} in that. Try something like: "
                       "“I use a wheelchair, my budget is $3,500, I work at 350 5th Ave, Manhattan”."}
        return
    g = engine.geocode(p["work"])
    if "error" in g:
        yield {"type": "step", "label": "Could not locate the workplace", "detail": g["error"], "status": "warn", "kind": "geocode"}
        yield {"type": "answer", "model": None,
               "text": f"I could not find “{p['work']}” in New York City. Try a full street address, or one of: "
                       + ", ".join(g.get("hubs", [])) + "."}
        return
    yield {"type": "step", "label": "Located the workplace", "kind": "geocode", "status": "done",
           "detail": f"{p['work']} → {g['zone_name']}, {g['borough']} ({g['source']})"}
    label = p["work"][:80]
    res = engine.score(p["budget"], g["lat"], g["lon"], label)
    c = res["counts"]
    yield {"type": "step", "label": "Scored all 263 zones", "kind": "score", "status": "done",
           "detail": f"{c['affordable']} within budget → {c['qualifying']} also have an accessible station; {c['deserts']} deserts"}
    yield {"type": "result", "data": res}
    yield _repro_check(res)
    for purpose, sql in (
        ("How many zones pass each filter", f"SELECT count(*) FILTER (rent_zori IS NULL) AS no_rent_data, count(*) FILTER (rent_zori <= {p['budget']:.0f} AND mf_units >= 1000) AS within_budget, count(*) FILTER (rent_zori <= {p['budget']:.0f} AND mf_units >= 1000 AND ada_stations_800m > 0) AS with_ada_station FROM zone_features"),
        ("Elevator record of the top zone's home station, month by month",
         None if not res["top3"] or not res["top3"][0]["home_station"] else
         f"SELECT strftime(e.month, '%Y-%m') AS month, count(*) AS elevators, round(avg(e.avail_24h), 3) AS avg_availability, round(min(e.avail_24h), 3) AS worst_elevator, sum(e.unscheduled_outages) AS unscheduled_outages FROM elevator_raw e JOIN stations s USING (complex_id) WHERE s.station_id = {res['top3'][0]['home_station']['station_id']} GROUP BY 1 ORDER BY 1"),
    ):
        if sql:
            out = engine.run_sql(sql)
            yield {"type": "sql", "purpose": purpose, "sql": sql, "by": "rules", "columns": out.get("columns"),
                   "rows": (out.get("rows") or [])[:10], "row_count": out.get("row_count"), "error": out.get("error")}
    mid, png = _store_map(res, "score")
    yield {"type": "step", "label": "Rendered the map", "kind": "map", "status": "warn",
           "detail": "No model connected, so nobody looked at it — the visual self-check was skipped"}
    yield {"type": "map", "id": mid, "view": "score", "seen_by_model": False}
    if not res["top3"]:
        text = (f"No zone has a typical rent within ${p['budget']:,.0f} together with an accessible subway station in reach. "
                "Try a higher budget — the table below updates as you move the budget dial.")
    else:
        t = res["top3"]
        first = t[0]
        text = (f"With ${p['budget']:,.0f} a month and work at {label}, **{first['zone_name']}** ({first['borough']}) "
                f"scores highest at {first['score']:.0f}/100: {first['pluses'][0][0].lower() + first['pluses'][0][1:]}. "
                f"Its biggest risk: {first['risk'][0].lower() + first['risk'][1:]}."
                + (f" Next are **{t[1]['zone_name']}** ({t[1]['score']:.0f})" + (f" and **{t[2]['zone_name']}** ({t[2]['score']:.0f})" if len(t) > 2 else "") + "." if len(t) > 1 else "")
                + "\n\n**Map check:** skipped — this run used fixed rules because no Claude API key is configured, "
                  "so no model looked at the map."
                + "\n\n**Keep in mind:** rents are typical market rents for the area and the housing figures are proxies; "
                  "none of this confirms that an accessible apartment is available.")
        if res["notes"]:
            text = res["notes"][0] + "\n\n" + text
    yield {"type": "answer", "text": text, "model": None}


# ------------------------------------------------------------------- entrypoint
def _cache_file(question: str) -> Path:
    question = question.strip()[:1000]
    norm = re.sub(r"\s+", " ", question.lower())
    if MODEL != "claude-fable-5-2":  # keep existing Fable cache keys; other models get their own
        norm += f"|{MODEL}"
    key = hashlib.sha1(norm.encode()).hexdigest()[:16]
    return CACHE_DIR / f"run_{key}.json"


def is_cached(question: str) -> bool:
    return _cache_file(question).exists()


def run(question: str, fresh: bool = False) -> Iterator[dict]:
    t0 = time.time()
    question = question.strip()[:1000]
    cache_file = _cache_file(question)
    live = has_credentials()

    if live and not fresh and cache_file.exists():
        cached = json.loads(cache_file.read_text())
        yield {"type": "mode", "mode": "fable", "model": MODEL, "cached_at": cached["at"]}
        yield from cached["events"]
        yield {"type": "done", "seconds": round(time.time() - t0, 2), "cached": True}
        return

    events: list[dict] = []
    if live:
        yield {"type": "mode", "mode": "fable", "model": MODEL}
        try:
            for ev in _run_fable(question):
                if ev["type"] not in ("answer_delta", "answer_reset") and not (ev["type"] == "step" and ev.get("status") == "active"):
                    events.append(ev)
                yield ev
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps({"at": time.strftime("%H:%M"), "question": question, "events": events}))
            yield {"type": "done", "seconds": round(time.time() - t0, 1)}
            return
        except Exception as e:  # auth, network, rate limit, refusal … → keep the demo alive on the rule-based path
            import anthropic
            if isinstance(e, anthropic.AuthenticationError):
                why = "The Claude API rejected the key"
            elif isinstance(e, anthropic.RateLimitError):
                why = "The Claude API is rate-limiting this key"
            elif isinstance(e, anthropic.APIStatusError):
                why = f"Claude API error {e.status_code}: {str(e.message)[:160]}"
            elif isinstance(e, anthropic.APIConnectionError):
                why = "Could not reach the Claude API"
            else:
                why = f"Agent stopped: {e}"[:220]
            yield {"type": "answer_reset"}
            yield {"type": "mode", "mode": "offline", "reason": why + " — continuing with fixed rules."}
            yield from _run_offline(question, why + " — continuing with fixed rules.")
    else:
        why = "No Claude API key found (set ANTHROPIC_API_KEY or put it in .env) — using fixed rules instead of the model."
        yield {"type": "mode", "mode": "offline", "reason": why}
        yield from _run_offline(question, why)
    yield {"type": "done", "seconds": round(time.time() - t0, 1)}
