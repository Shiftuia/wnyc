"""Step 3 — web server. Run:  .venv/bin/python app/server.py   → http://127.0.0.1:8765"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssl  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

import agent  # noqa: E402
import engine  # noqa: E402
import router  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(title="Wheelchair NYC", docs_url=None, redoc_url=None, openapi_url=None)

# Content-Security-Policy: scripts only from this origin and the first-party Umami host; no inline script, no
# plugins, no framing. Model output is rendered through esc() (map.js); this is the second line of defence.
UMAMI = "https://metrics.shiftuia.com"
CSP = ("default-src 'self'; "
       f"script-src 'self' {UMAMI}; "
       f"connect-src 'self' {UMAMI}; "
       "img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; "
       "worker-src 'self' blob:; child-src blob:; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.get("/health")
def health():
    """Liveness + data readiness: the prepared database opens and has its zones."""
    try:
        n = engine.q("SELECT count(*) AS n FROM zones")[0]["n"]
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
    return JSONResponse({"ok": n > 0, "zones": n, "model": agent.MODEL, "live": agent.has_credentials(),
                         "revision": os.environ.get("WNYC_GIT_REVISION", "unset")},
                        status_code=200 if n > 0 else 503, headers={"Cache-Control": "no-store"})


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    fresh: bool = False


class ScoreReq(BaseModel):
    budget: float = Field(ge=300, le=50000)
    work_lat: float = Field(gt=40.4, lt=41.0)
    work_lon: float = Field(gt=-74.3, lt=-73.6)
    work_label: str | None = Field(default=None, max_length=80)


def _page(name: str):
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


@app.get("/")
def index():          # short landing page
    return _page("index.html")


@app.get("/map")
def map_app():        # the map-centric app
    return _page("map.html")


@app.get("/api/bootstrap")
def bootstrap():
    """Static layers: zone shapes, accessible stations, data checks, hubs, mode."""
    return JSONResponse({
        "zones": engine.zones_geojson(),
        "stations": engine.stations_for_map(),
        "checks": engine.q("SELECT name, value, detail, ok FROM data_checks"),
        "hubs": [h["name"] for h in engine.HUBS],
        "weights": engine.WEIGHTS,
        "live": agent.has_credentials(),
        "landing": landing(),
        "zone_info": engine.q("""SELECT f.zone_id, f.zone_name, f.borough, f.rent_zori, f.mf_units, f.ada_stations_800m, f.ada_station_names,
                                        f.ada_elevator_uptime, f.units_near_ada_share, f.elevator_units_share, f.fha_units_share,
                                        f.wav_wait_median_min, f.ramp_good_share, e.*,
                                        u.upgrade_names
                                 FROM zone_features f JOIN zone_extra e USING (zone_id) LEFT JOIN zone_upgrades u USING (zone_id)"""),
        "upgrades": engine.q("SELECT stop_name AS name, routes, strftime(forecast, '%b %Y') AS forecast, project, lat, lon FROM station_upgrades"),
        "outages": engine.q("""SELECT o.complex_id, o.equipment, o.station, o.serving, o.reason, o.upcoming, strftime(o.starts, '%d %b %Y') AS starts,
                                      strftime(o.returns, '%d %b %Y') AS returns, o.days
                               FROM elevator_outages o WHERE NOT o.upcoming OR o.days >= 1.5"""),
        "feed_time": engine._static()["feed_time"],
        "fares": router.FARES, "periods": router.PERIODS,
        "model": agent.MODEL,
        "city": engine.q("""SELECT (SELECT count(*) FROM stations) AS stations,
                                   (SELECT count(*) FROM stations WHERE ada = 1) AS ada_full,
                                   (SELECT count(*) FROM stations WHERE ada = 2) AS ada_partial,
                                   (SELECT round(min(elevator_uptime), 3) FROM stations WHERE ada IN (1, 2)) AS worst_uptime,
                                   (SELECT stop_name FROM stations WHERE ada IN (1, 2) ORDER BY elevator_uptime LIMIT 1) AS worst_station""")[0],
    })


CITY_SQL = """SELECT (SELECT count(*) FROM stations) AS stations,
                     (SELECT count(*) FROM stations WHERE ada = 1) AS ada_full,
                     (SELECT count(*) FROM stations WHERE ada = 2) AS ada_partial,
                     (SELECT round(min(elevator_uptime), 3) FROM stations WHERE ada IN (1, 2)) AS worst_uptime,
                     (SELECT stop_name FROM stations WHERE ada IN (1, 2) ORDER BY elevator_uptime LIMIT 1) AS worst_station"""


@app.get("/api/landing")
def landing_api():
    """Small payload for the landing page (no geometry)."""
    d = landing()
    d.pop("has_station", None)
    return JSONResponse({**d, "city": engine.q(CITY_SQL)[0], "live": agent.has_credentials()})


def landing() -> dict:
    """Numbers for the landing section — all computed from the prepared tables, except the Census count."""
    cov = engine.q("""SELECT count(*) AS zones, count(*) FILTER (ada_stations_800m = 0) AS zones_no_station,
                             sum(mf_units) AS units,
                             round(sum(mf_units * units_near_ada_share)) AS units_near_ada
                      FROM zone_features WHERE mf_units >= 1000""")[0]
    pair = engine.q("""
        SELECT a.zone_id AS a_id, a.zone_name AS a_name, a.rent_zori AS a_rent, a.ada_station_names AS a_stations,
               a.ada_stations_800m AS a_n, a.ada_elevator_uptime AS a_uptime, a.units_near_ada_share AS a_near,
               a.wav_wait_median_min AS a_wav,
               b.zone_id AS b_id, b.zone_name AS b_name, b.rent_zori AS b_rent, b.units_near_ada_share AS b_near,
               b.wav_wait_median_min AS b_wav, b.wav_wait_p90_min AS b_wav_p90, b.mf_units AS b_units, a.borough
        FROM zone_features a JOIN zone_features b ON a.borough = b.borough AND abs(a.rent_zori - b.rent_zori) <= 40
        WHERE a.mf_units >= 5000 AND b.mf_units >= 5000 AND a.ada_stations_800m >= 2 AND a.units_near_ada_share >= 0.85
          AND a.ada_elevator_uptime >= 0.97 AND a.rent_zori <= 3500
          AND b.ada_stations_800m = 0 AND b.units_near_ada_share < 0.05 AND b.wav_wait_median_min >= 5.5
        ORDER BY b.mf_units DESC, a.zone_id LIMIT 1""")
    return {
        "ambulatory": {"n": 582712, "source": "American Community Survey 2019–2023, via NYC Mayor’s Office for People with Disabilities"},
        "coverage": cov,
        "has_station": {r["zone_id"]: r["n"] for r in engine.q(
            "SELECT zone_id, CASE WHEN mf_units < 1000 THEN -1 ELSE ada_stations_800m END AS n FROM zone_features")},
        "waits": engine.q("SELECT * FROM city_stats")[0],
        "pair": pair[0] if pair else None,
    }


# Limits (public demo on someone's API key). The ingress sidecar sets X-WNYC-Client from Cloudflare's
# CF-Connecting-IP (or the TCP peer) and is the app's only HTTP peer in the container stack, so that header is
# trusted only when WNYC_TRUST_CLIENT_HEADER=1. X-Forwarded-For is never used: its left part is client-controlled.
PER_IP_HOUR = int(os.environ.get("WNYC_ASK_PER_IP_HOUR", "20"))
PER_DAY = int(os.environ.get("WNYC_ASK_PER_DAY", "2000"))
LOOKUPS_PER_IP_HOUR = int(os.environ.get("WNYC_LOOKUPS_PER_IP_HOUR", "120"))   # geocode + trip (third-party APIs)
ASK_CONCURRENCY = int(os.environ.get("WNYC_ASK_CONCURRENCY", "2"))
TRUST_CLIENT_HEADER = os.environ.get("WNYC_TRUST_CLIENT_HEADER") == "1"
_hits: dict[str, dict[str, deque]] = {"ask": defaultdict(deque), "lookup": defaultdict(deque)}
_day = {"date": "", "n": 0}
_lim_lock = threading.Lock()
_ask_slots = threading.BoundedSemaphore(ASK_CONCURRENCY)


def client_key(request: Request) -> str:
    if TRUST_CLIENT_HEADER:
        v = request.headers.get("x-wnyc-client", "").strip()
        if v:
            return v[:64]
    return request.client.host if request.client else "?"


def _hit(kind: str, ip: str, per_hour: int) -> bool:
    """Record one request; False when this IP already made per_hour of them in the last hour."""
    now = time.time()
    with _lim_lock:
        table = _hits[kind]
        if len(table) > 20000:  # forget idle visitors so the table stays bounded
            for k in [k for k, h in table.items() if not h or h[-1] < now - 3600]:
                del table[k]
        h = table[ip]
        while h and h[0] < now - 3600:
            h.popleft()
        if len(h) >= per_hour:
            return False
        h.append(now)
        return True


def _over_limit(ip: str) -> str | None:
    with _lim_lock:
        today = time.strftime("%Y-%m-%d")
        if _day["date"] != today:
            _day.update(date=today, n=0)
        if _day["n"] >= PER_DAY:
            return "The demo has used up today's questions. Please come back tomorrow."
    if not _hit("ask", ip, PER_IP_HOUR):
        return f"You have asked {PER_IP_HOUR} questions in the last hour. Please try again a bit later."
    with _lim_lock:
        _day["n"] += 1
    return None


def _lookup_allowed(request: Request) -> None:
    if not _hit("lookup", client_key(request), LOOKUPS_PER_IP_HOUR):
        raise HTTPException(429, "Too many address lookups from your connection. Please try again later.")


def _error_stream(message: str):
    yield json.dumps({"type": "error", "message": message}) + "\n"
    yield json.dumps({"type": "done", "seconds": 0}) + "\n"


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    cached = agent.is_cached(body.question) and not body.fresh and agent.has_credentials()
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    why = None if cached else _over_limit(client_key(request))
    if why:
        return StreamingResponse(_error_stream(why), media_type="application/x-ndjson", headers=headers)

    def gen():
        # the slot is taken inside the generator so a client that disconnects before streaming starts cannot leak it
        if not _ask_slots.acquire(blocking=False):
            yield from _error_stream("The demo is busy answering other questions. Please try again in a minute.")
            return
        try:
            for ev in agent.run(body.question, fresh=body.fresh):
                yield json.dumps(ev, default=str) + "\n"
        except Exception as e:  # never leave the client hanging mid-stream
            yield from _error_stream(f"{type(e).__name__}: {e}"[:300])
        finally:
            _ask_slots.release()
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers=headers)


@app.post("/api/score")
def score(body: ScoreReq):
    """Instant deterministic re-rank (budget dial, hub switch) — no model call."""
    return JSONResponse(json.loads(json.dumps(
        engine.score(body.budget, body.work_lat, body.work_lon, body.work_label), default=str)))


class TripPoint(BaseModel):
    lat: float = Field(gt=40.4, lt=41.0)
    lon: float = Field(gt=-74.3, lt=-73.6)
    label: str | None = Field(default=None, max_length=120)


class TripReq(BaseModel):
    origin: TripPoint | str = Field(union_mode="left_to_right")
    destination: TripPoint | str = Field(union_mode="left_to_right")
    when: Literal["am_peak", "midday", "pm_peak", "night", "weekend"] = "am_peak"
    chair: Literal["manual", "power", "walker"] = "manual"
    optimise: Literal["time", "cost", "effort"] = "time"


def _pt(p: TripPoint | str):
    return p.model_dump(exclude_none=True) if isinstance(p, TripPoint) else p[:200]


@app.post("/api/trip")
def trip(body: TripReq, request: Request):
    _lookup_allowed(request)
    a, b = router.locate(_pt(body.origin)), router.locate(_pt(body.destination))
    for which, p in (("start", a), ("destination", b)):
        if "error" in p:
            return JSONResponse({"error": f"Could not find the {which}: {p['error']}", "hubs": p.get("hubs")}, status_code=422)
    return JSONResponse(json.loads(json.dumps(router.add_paths(router.plan(a, b, body.when, body.chair, body.optimise)), default=str)))


@app.get("/api/geocode")
def geocode(q: str, request: Request):
    _lookup_allowed(request)
    return JSONResponse(engine.geocode(q[:200]))


@app.get("/api/map/{mid}.png")
def map_png(mid: str):
    png = agent.load_map(mid)
    if png is None:
        raise HTTPException(404)
    return Response(png, media_type="image/png")


# ------------------------------------------------------------------ offline street map
# scripts/fetch_basemap.py saves the NYC map into data/basemap/. Files are served from disk; anything missing
# (an area outside NYC, a rare glyph range) is fetched once from OpenFreeMap when online and kept.
BASEMAP = engine.DATA / "basemap"
# WNYC_BASEMAP_UPSTREAM=0 (the container default): never fetch from OpenFreeMap, serve the saved files only
_UP = (json.loads((BASEMAP / "upstream.json").read_text())
       if os.environ.get("WNYC_BASEMAP_UPSTREAM", "1") != "0" and (BASEMAP / "upstream.json").exists() else None)


def _cached(path: Path, upstream: str | None, media: str) -> Response:
    root = BASEMAP.resolve()
    path = path.resolve()
    if root not in path.parents:
        raise HTTPException(404)
    if not path.exists() and upstream:
        try:
            req = urllib.request.Request(upstream, headers={"User-Agent": "wheelchair-nyc-demo/1.0"})
            with urllib.request.urlopen(req, timeout=4, context=engine._SSL) as r:
                body = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        except Exception:
            pass
    if not path.exists():
        return Response(status_code=204)          # MapLibre treats an empty tile as "nothing here"
    return Response(path.read_bytes(), media_type=media, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/basemap/style.json")
def basemap_style(request: Request):
    f = BASEMAP / "style.json"
    if not f.exists():
        raise HTTPException(404, "run scripts/fetch_basemap.py")
    return Response(f.read_text().replace("{ORIGIN}", str(request.base_url).rstrip("/")), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.get("/basemap/tiles/{z}/{x}/{y}.pbf")
def basemap_tile(z: int, x: int, y: int):
    if not (0 <= z <= 14 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404)
    up = _UP["tiles"].format(z=z, x=x, y=y) if _UP else None
    return _cached(BASEMAP / "tiles" / str(z) / str(x) / f"{y}.pbf", up, "application/vnd.mapbox-vector-tile")


@app.get("/basemap/fonts/{fontstack}/{rng}.pbf")
def basemap_font(fontstack: str, rng: str):
    if (len(fontstack) > 100 or not all(c.isalnum() or c in " -," for c in fontstack)
            or not re.fullmatch(r"\d{1,5}-\d{1,5}", rng)):
        raise HTTPException(404)
    first = fontstack.split(",")[0].strip()
    up = _UP["fonts"].replace("{fontstack}", urllib.parse.quote(first)).replace("{range}", rng) if _UP else None
    return _cached(BASEMAP / "fonts" / first / f"{rng}.pbf", up, "application/x-protobuf")


@app.get("/basemap/sprite/{name}")
def basemap_sprite(name: str):
    if name not in ("ofm.json", "ofm.png", "ofm@2x.json", "ofm@2x.png"):
        raise HTTPException(404)
    up = _UP["sprite"] + name[3:] if _UP else None
    return _cached(BASEMAP / "sprite" / name, up, "application/json" if name.endswith(".json") else "image/png")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if __name__ == "__main__":
    import uvicorn
    # proxy_headers off: client identity for limits comes from client_key(), never from X-Forwarded-For;
    # access log off: request lines carry visitor addresses (/api/geocode?q=)
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8765")), log_level="warning",
                proxy_headers=False, server_header=False, access_log=False, limit_concurrency=64)
