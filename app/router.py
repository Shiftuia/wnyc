"""A → B trip check for a wheelchair user: every way to make the trip, with time, cost, effort and whether it
can be done alone.

What is real data:   subway run times and headways (MTA timetable), which stations are accessible, elevator
                     availability and current outages, Uber/Lyft duration and fare between zones by time of day
                     (21M trips, so traffic is in the numbers), accessible-vehicle pickup waits.
What is an assumption (shown in the UI): wheeling speed, a 1.3 detour factor on straight-line distance,
                     3 minutes per elevator trip between street and platform, and the "solo / help" thresholds.
Not modelled: buses (no travel-time data here), platform gaps, street slope, door-to-door sidewalks.
"""
from __future__ import annotations

import heapq
import json
import math
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import engine

FARES = {  # verified on mta.info, Nov 2026
    "subway": 3.00, "subway_reduced": 1.50, "weekly_cap": 35.00, "access_a_ride": 3.00,
    "source": "MTA fares (mta.info/fares-tolls/subway-bus and /reduced-fare), in effect since 4 Jan 2026",
}
CHAIRS = {
    "manual": {"label": "manual wheelchair", "kmh": 3.2, "reach_m": 1200, "tiring_m": 800, "help_m": 1500, "solo_max_km": 3.0},
    "power": {"label": "power wheelchair", "kmh": 5.5, "reach_m": 2500, "tiring_m": 2500, "help_m": 5000, "solo_max_km": 8.0},
    # walks with a cane or walker: short distances, no stairs — still needs step-free stations
    "walker": {"label": "cane or walker", "kmh": 2.5, "reach_m": 600, "tiring_m": 400, "help_m": 800, "solo_max_km": 1.2, "verb": "Walk"},
}
PERIODS = {"am_peak": "weekday morning rush (7–10)", "midday": "weekday midday (10–16)", "pm_peak": "weekday evening rush (16–19)",
           "night": "weekday night", "weekend": "weekend"}
DETOUR = 1.3
ELEV_S = 180          # street ↔ platform by elevator, each end
XFER_SAME_S = 180     # change of route inside one accessible station
ROUTE_NAME = {"6X": "6", "7X": "7", "FX": "F", "GS": "S", "FS": "S", "H": "S", "SI": "SIR", "5X": "5"}


def _m(a_lat, a_lon, b_lat, b_lon) -> float:
    dy = (a_lat - b_lat) * 111_320
    dx = (a_lon - b_lon) * 111_320 * math.cos(math.radians(40.7))
    return math.hypot(dx, dy)


@lru_cache(maxsize=1)
def _net() -> dict:
    st = engine.q("""SELECT s.station_id, g.gtfs_stop_id AS stop, s.complex_id, s.stop_name AS name, s.routes, s.ada, s.ada_notes,
                            s.lat, s.lon, s.zone_id, s.elevator_uptime AS uptime, s.uptime_worst_elevator AS uptime_worst
                     FROM stations s JOIN station_gtfs g USING (station_id)""")
    by_stop = {s["stop"]: s for s in st}
    adj: dict[tuple, list] = {}
    routes_at: dict[str, set] = {}
    for e in engine.q("SELECT route, from_stop, to_stop, secs FROM subway_edges"):
        adj.setdefault((e["from_stop"], e["route"]), []).append((e["to_stop"], e["secs"]))
        routes_at.setdefault(e["from_stop"], set()).add(e["route"])
        routes_at.setdefault(e["to_stop"], set()).add(e["route"])
    xfer: dict[str, list] = {}
    for t in engine.q("SELECT from_stop, to_stop, secs FROM subway_transfers"):
        xfer.setdefault(t["from_stop"], []).append((t["to_stop"], t["secs"]))
    headway = {r["route"]: r["headway_min"] for r in engine.q("SELECT route, headway_min FROM route_headway")}
    outages: dict[int, list] = {}
    for o in engine.q("""SELECT complex_id, equipment, serving, reason, strftime(returns, '%d %b') AS returns, days
                         FROM elevator_outages WHERE NOT upcoming ORDER BY days DESC"""):
        outages.setdefault(o["complex_id"], []).append(o)
    planned = {r["complex_id"]: r["n"] for r in engine.q("SELECT complex_id, count(*) AS n FROM elevator_outages WHERE upcoming GROUP BY 1")}
    backup = {r["complex_id"]: r for r in engine.q("SELECT * FROM elevator_redundancy")}
    model = {r["period"]: r for r in engine.q("SELECT * FROM ride_model")}
    wav = {r["zone_id"]: r for r in engine.q("SELECT zone_id, wav_wait_median_min, wav_wait_p90_min FROM zone_features")}
    city = engine.q("SELECT * FROM city_stats")[0]
    fetched = engine.q("SELECT strftime(elevator_feed_fetched, '%d %b %H:%M') AS t FROM feed_info")[0]["t"]
    shapes: dict[str, dict[str, list]] = {}
    for r in engine.q("SELECT route, shape_id, lat, lon FROM subway_shapes ORDER BY shape_id, seq"):
        shapes.setdefault(r["route"], {}).setdefault(r["shape_id"], []).append((r["lat"], r["lon"]))
    return {"shapes": shapes, "stations": st, "by_stop": by_stop, "adj": adj, "routes_at": routes_at, "xfer": xfer, "headway": headway,
            "outages": outages, "planned": planned, "backup": backup, "model": model, "wav": wav, "city": city,
            "ada": [s for s in st if s["ada"] in (1, 2)], "feed_time": fetched}


def _track(net, route_id: str, stops: list[tuple]) -> list[list[float]] | None:
    """The real track geometry between the first and last stop of a subway leg (GTFS shape, cut to the leg)."""
    if len(stops) < 2:
        return None
    a, b, best = stops[0], stops[-1], None
    for rid, by_shape in net["shapes"].items():
        if rid != route_id and ROUTE_NAME.get(rid, rid) != ROUTE_NAME.get(route_id, route_id):
            continue
        for pts in by_shape.values():
            i = min(range(len(pts)), key=lambda k: (pts[k][0] - a[0]) ** 2 + (pts[k][1] - a[1]) ** 2)
            j = min(range(len(pts)), key=lambda k: (pts[k][0] - b[0]) ** 2 + (pts[k][1] - b[1]) ** 2)
            if i >= j:
                continue
            d = _m(a[0], a[1], *pts[i]) + _m(b[0], b[1], *pts[j])
            if d < 400 and (best is None or d < best[0]):
                best = (d, pts[i:j + 1])
    return [[p[0], p[1]] for p in best[1]] if best else None


_road_cache_path = engine.DATA / "route_cache.json"
_road_lock = threading.Lock()
_road_cache: dict | None = None


def _road(a: list, b: list, profile: str) -> list[list[float]] | None:
    """Street geometry between two points from the public OSM routing service (only used for drawing —
    times and fares still come from the trip data). Returns None when the service is unreachable."""
    global _road_cache
    key = f"{profile}:{a[0]:.5f},{a[1]:.5f}>{b[0]:.5f},{b[1]:.5f}"
    with _road_lock:
        if _road_cache is None:
            try:
                _road_cache = json.loads(_road_cache_path.read_text())
            except Exception:
                _road_cache = {}
        if key in _road_cache:
            return _road_cache[key]
    sub = {"car": "routed-car/route/v1/driving", "foot": "routed-foot/route/v1/foot"}[profile]
    url = f"https://routing.openstreetmap.de/{sub}/{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wheelchair-nyc-demo/1.0"})
        with urllib.request.urlopen(req, timeout=5, context=engine._SSL) as r:
            coords = json.loads(r.read().decode())["routes"][0]["geometry"]["coordinates"]
        path = [[round(c[1], 6), round(c[0], 6)] for c in coords]
    except Exception:
        return None
    with _road_lock:
        _road_cache[key] = path
        if len(_road_cache) > 400:
            for k in list(_road_cache)[:100]:
                del _road_cache[k]
        _road_cache_path.write_text(json.dumps(_road_cache))
    return path


def add_paths(result: dict) -> dict:
    """Attach drawable street paths to every car and wheel/walk step (in parallel, best effort)."""
    jobs = [(s, "car" if s["mode"] == "car" else "foot") for o in result["options"] for s in o["steps"]
            if s["mode"] in ("car", "wheel") and s.get("from_pt") and s.get("to_pt")]
    if jobs:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for (s, _), path in zip(jobs, ex.map(lambda j: _road(j[0]["from_pt"], j[0]["to_pt"], j[1]), jobs)):
                if path and len(path) > 1:
                    s["path"] = path
    result["paths_from"] = "Subway lines follow the MTA track geometry; street paths come from OpenStreetMap routing (drawing only)."
    return result


def _wait_s(net, route) -> float:
    return 30 * net["headway"].get(route, 10)   # half the headway, in seconds


def _subway(net, origins: dict[str, float], dests: dict[str, float]) -> dict | None:
    """Dijkstra over (stop, route). origins/dests: stop → access/egress seconds. Boarding, alighting and
    changing trains are only allowed at accessible stations."""
    pq, best, parent = [], {}, {}
    for stop, acc in origins.items():
        for r in net["routes_at"].get(stop, ()):
            c = acc + ELEV_S + _wait_s(net, r)
            if c < best.get((stop, r), 1e18):
                best[(stop, r)] = c
                parent[(stop, r)] = None
                heapq.heappush(pq, (c, stop, r))
    goal, goal_cost = None, 1e18
    while pq:
        c, stop, r = heapq.heappop(pq)
        if c > best.get((stop, r), 1e18):
            continue
        if c >= goal_cost:
            break
        if stop in dests and parent[(stop, r)] is not None and c + ELEV_S + dests[stop] < goal_cost:
            goal, goal_cost = (stop, r), c + ELEV_S + dests[stop]
        for nxt, secs in net["adj"].get((stop, r), ()):
            nc = c + secs
            if nc < best.get((nxt, r), 1e18):
                best[(nxt, r)] = nc
                parent[(nxt, r)] = ((stop, r), "ride", secs)
                heapq.heappush(pq, (nc, nxt, r))
        s = net["by_stop"].get(stop)
        if s and s["ada"] == 1:   # change trains only where the whole station is accessible
            for r2 in net["routes_at"].get(stop, ()):
                if r2 != r and ROUTE_NAME.get(r2, r2) != ROUTE_NAME.get(r, r):
                    nc = c + XFER_SAME_S + _wait_s(net, r2)
                    if nc < best.get((stop, r2), 1e18):
                        best[(stop, r2)] = nc
                        parent[(stop, r2)] = ((stop, r), "transfer", XFER_SAME_S + _wait_s(net, r2))
                        heapq.heappush(pq, (nc, stop, r2))
            for stop2, secs in net["xfer"].get(stop, ()):
                s2 = net["by_stop"].get(stop2)
                if s2 and s2["ada"] == 1 and s2["complex_id"] == s["complex_id"]:
                    for r2 in net["routes_at"].get(stop2, ()):
                        add = secs + 120 + _wait_s(net, r2)
                        if c + add < best.get((stop2, r2), 1e18):
                            best[(stop2, r2)] = c + add
                            parent[(stop2, r2)] = ((stop, r), "transfer", add)
                            heapq.heappush(pq, (c + add, stop2, r2))
    if goal is None:
        return None
    # rebuild legs
    chain, node = [], goal
    while parent[node] is not None:
        prev, kind, secs = parent[node]
        chain.append((prev, node, kind, secs))
        node = prev
    chain.reverse()
    first = node
    legs, cur = [], None
    for prev, nxt, kind, secs in chain:
        if kind == "ride":
            if cur is None:
                cur = {"mode": "subway", "route_id": prev[1], "route": ROUTE_NAME.get(prev[1], prev[1]), "from_stop": prev[0], "to_stop": nxt[0], "secs": 0, "stops": 0, "path": [prev[0]]}
            cur["to_stop"] = nxt[0]
            cur["secs"] += secs
            cur["stops"] += 1
            cur["path"].append(nxt[0])
        else:
            if cur:
                legs.append(cur)
                cur = None
            legs.append({"mode": "transfer", "at_stop": prev[0], "to_stop": nxt[0], "secs": secs, "to_route": ROUTE_NAME.get(nxt[1], nxt[1])})
    if cur:
        legs.append(cur)
    return {"board": first[0], "alight": goal[0], "first_route": first[1], "legs": legs,
            "secs": goal_cost, "first_wait_s": _wait_s(net, first[1])}


def _ride(net, zone_a, zone_b, period: str, metres: float) -> dict:
    """Uber/Lyft between two zones: this period's trips, else all-day trips, else a fitted per-mile model."""
    r = engine.q("SELECT trips, fare_median, minutes_median FROM od_fare_period WHERE pu_zone = ? AND do_zone = ? AND period = ?", [zone_a, zone_b, period])
    if r:
        out = {"minutes": r[0]["minutes_median"], "fare": r[0]["fare_median"], "basis": f"typical of {r[0]['trips']:,} trips on this route, {PERIODS[period]}"}
    else:
        r = engine.q("SELECT trips, fare_median, minutes_median FROM od_fare WHERE pu_zone = ? AND do_zone = ?", [zone_a, zone_b])
        if r:
            out = {"minutes": r[0]["minutes_median"], "fare": r[0]["fare_median"], "basis": f"typical of {r[0]['trips']:,} trips on this route, all times of day"}
        else:
            m, miles = net["model"][period], metres * DETOUR / 1609.34
            out = {"minutes": round(m["min_base"] + m["min_per_mile"] * miles, 1), "fare": round(m["fare_base"] + m["fare_per_mile"] * miles, 2),
                   "basis": f"estimate from distance — too few trips on this exact route ({PERIODS[period]})"}
    w = net["wav"].get(zone_a) or {}
    out["wait"] = w.get("wav_wait_median_min") or round(net["city"]["wav_wait_median_min"], 1)
    out["wait_p90"] = w.get("wav_wait_p90_min") or round(net["city"]["wav_wait_p90_min"], 1)
    out["wait_basis"] = "this pickup zone" if w.get("wav_wait_median_min") else "citywide (too few accessible rides here)"
    return out


def _st(net, stop) -> dict:
    s = net["by_stop"][stop]
    cur = net["outages"].get(s["complex_id"], [])
    b = net["backup"].get(s["complex_id"])
    return {"name": s["name"], "routes": s["routes"].split(), "ada": s["ada"], "ada_notes": s["ada_notes"], "lat": s["lat"], "lon": s["lon"],
            "uptime": s["uptime"], "complex_id": s["complex_id"],
            "out_now": [{"equipment": o["equipment"], "serving": o["serving"], "reason": o["reason"], "returns": o["returns"], "days": o["days"]} for o in cur],
            "planned_outages": net["planned"].get(s["complex_id"], 0),
            "no_backup": bool(b and b["no_backup"] > 0)}


def _near(net, lat, lon, radius_m, full_only=False, limit=None) -> list[tuple[dict, float]]:
    out = [(s, _m(lat, lon, s["lat"], s["lon"])) for s in net["ada"] if not (full_only and s["ada"] != 1)]
    out = sorted([x for x in out if x[1] <= radius_m], key=lambda x: x[1])
    return out[:limit] if limit else out


def plan(a: dict, b: dict, when: str = "am_peak", chair: str = "manual", optimise: str = "time") -> dict:
    """a, b: {lat, lon, label, zone_id}. Returns every option, feasible or not, with reasons."""
    net, ch = _net(), CHAIRS.get(chair, CHAIRS["manual"])
    when = when if when in PERIODS else "am_peak"
    wheel_s = lambda m: m * DETOUR / (ch["kmh"] / 3.6)          # noqa: E731
    direct = _m(a["lat"], a["lon"], b["lat"], b["lon"])
    options: list[dict] = []

    def wheel_leg(frm, to, metres, p_from=None, p_to=None):
        return {"mode": "wheel", "verb": ch.get("verb", "Wheel"), "from": frm, "to": to, "from_pt": p_from, "to_pt": p_to, "metres": round(metres * DETOUR), "minutes": round(wheel_s(metres) / 60, 1)}

    def subway_option(key, title, origins, dests, pre=None, post=None, extra_cost=0.0, pre_min=0.0, post_min=0.0):
        res = _subway(net, origins, dests)
        if not res:
            return None
        steps, stations_used = list(pre or []), []
        board, alight = _st(net, res["board"]), _st(net, res["alight"])
        stations_used.append(board)
        if pre is None:
            steps.append(wheel_leg(a["label"], board["name"] + " station", origins[res["board"]] / wheel_s(1) if wheel_s(1) else 0,
                                   [a["lat"], a["lon"]], [board["lat"], board["lon"]]))
        steps.append({"mode": "elevator", "at": board["name"], "minutes": ELEV_S / 60, "wait_train_min": round(res["first_wait_s"] / 60, 1)})
        for leg in res["legs"]:
            if leg["mode"] == "subway":
                steps.append({"mode": "subway", "route": leg["route"], "from": net["by_stop"][leg["from_stop"]]["name"],
                              "to": net["by_stop"][leg["to_stop"]]["name"], "stops": leg["stops"], "minutes": round(leg["secs"] / 60, 1),
                              "path": _track(net, leg["route_id"], [(net["by_stop"][x]["lat"], net["by_stop"][x]["lon"]) for x in leg["path"] if x in net["by_stop"]])
                                      or [[net["by_stop"][x]["lat"], net["by_stop"][x]["lon"]] for x in leg["path"] if x in net["by_stop"]],
                              "from_pt": [net["by_stop"][leg["from_stop"]]["lat"], net["by_stop"][leg["from_stop"]]["lon"]],
                              "to_pt": [net["by_stop"][leg["to_stop"]]["lat"], net["by_stop"][leg["to_stop"]]["lon"]]})
            else:
                t = _st(net, leg["at_stop"])
                stations_used.append(t)
                steps.append({"mode": "transfer", "at": t["name"], "to_route": leg["to_route"], "minutes": round(leg["secs"] / 60, 1), "uptime": t["uptime"]})
        stations_used.append(alight)
        steps.append({"mode": "elevator", "at": alight["name"], "minutes": ELEV_S / 60})
        if post is None:
            steps.append(wheel_leg(alight["name"] + " station", b["label"], dests[res["alight"]] / wheel_s(1) if wheel_s(1) else 0,
                                   [alight["lat"], alight["lon"]], [b["lat"], b["lon"]]))
        else:
            steps += post
        total_min = res["secs"] / 60 + pre_min + post_min
        rel = 1.0
        for s in stations_used:
            rel *= s["uptime"] if s["uptime"] is not None else 1.0
        return {"key": key, "title": title, "possible": True, "minutes": round(total_min), "cost": round(FARES["subway"] + extra_cost, 2),
                "cost_reduced": round(FARES["subway_reduced"] + extra_cost, 2), "steps": steps, "stations": stations_used,
                "transfers": sum(1 for s in steps if s["mode"] == "transfer"),
                "elevator_reliability": round(rel, 3), "unknown_uptime": [s["name"] for s in stations_used if s["uptime"] is None]}

    # 1 — wheel the whole way
    if direct * DETOUR <= 10_000:
        km = direct * DETOUR / 1000
        ok = km <= ch["solo_max_km"]
        options.append({"key": "wheel", "title": f"{ch.get('verb', 'Wheel')} the whole way", "possible": ok, "minutes": round(wheel_s(direct) / 60), "cost": 0.0,
                        "steps": [wheel_leg(a["label"], b["label"], direct, [a["lat"], a["lon"]], [b["lat"], b["lon"]])], "stations": [], "transfers": 0,
                        "why_not": None if ok else f"About {km:.1f} km of sidewalk — beyond a realistic one-way trip with a {ch['label']} (limit used: {ch['solo_max_km']:g} km)"})

    # 2 — accessible subway, wheeling at both ends
    o_near, d_near = _near(net, a["lat"], a["lon"], ch["reach_m"]), _near(net, b["lat"], b["lon"], ch["reach_m"])
    if o_near and d_near:
        opt = subway_option("subway", "Accessible subway", {s["stop"]: wheel_s(m) for s, m in o_near}, {s["stop"]: wheel_s(m) for s, m in d_near})
        options.append(opt or {"key": "subway", "title": "Accessible subway", "possible": False, "steps": [], "stations": [], "transfers": 0,
                               "why_not": "Accessible stations exist at both ends, but no route links them using only accessible transfer stations"})
    else:
        ends = " or ".join(x for x, miss in (("the start", not o_near), ("the destination", not d_near)) if miss)
        options.append({"key": "subway", "title": "Accessible subway", "possible": False, "steps": [], "stations": [], "transfers": 0,
                        "why_not": f"No accessible subway station within {ch['reach_m'] / 1000:.1f} km of {ends}"})

    # 3 — accessible ride-hail door to door
    r = _ride(net, a["zone_id"], b["zone_id"], when, direct)
    car = {"mode": "car", "from": a["label"], "to": b["label"], "minutes": r["minutes"], "wait_min": r["wait"], "wait_p90": r["wait_p90"],
           "fare": r["fare"], "basis": r["basis"], "wait_basis": r["wait_basis"], "from_pt": [a["lat"], a["lon"]], "to_pt": [b["lat"], b["lon"]]}
    options.append({"key": "car", "title": "Accessible Uber/Lyft, door to door", "possible": True, "minutes": round(r["minutes"] + r["wait"]),
                    "cost": r["fare"], "steps": [car], "stations": [], "transfers": 0})

    # 4 — ride to an accessible station, then subway   /   5 — subway, then ride the last leg
    def hybrid(first: bool):
        far = _near(net, *( (a["lat"], a["lon"]) if first else (b["lat"], b["lon"]) ), 9000, full_only=True, limit=10)
        best = None
        for s, m in far:
            if m <= ch["reach_m"]:
                continue   # already covered by the plain subway option
            rr = _ride(net, a["zone_id"], s["zone_id"], when, m) if first else _ride(net, s["zone_id"], b["zone_id"], when, m)
            leg = {"mode": "car", "from": a["label"] if first else s["name"] + " station", "to": s["name"] + " station" if first else b["label"],
                   "minutes": rr["minutes"], "wait_min": rr["wait"], "wait_p90": rr["wait_p90"], "fare": rr["fare"], "basis": rr["basis"], "wait_basis": rr["wait_basis"],
                   "from_pt": [a["lat"], a["lon"]] if first else [s["lat"], s["lon"]], "to_pt": [s["lat"], s["lon"]] if first else [b["lat"], b["lon"]]}
            car_min = rr["minutes"] + rr["wait"]
            if first and d_near:
                opt = subway_option("car_subway", "Accessible Uber/Lyft to a station, then subway", {s["stop"]: 0.0},
                                    {x["stop"]: wheel_s(mm) for x, mm in d_near}, pre=[leg], extra_cost=rr["fare"], pre_min=car_min)
            elif not first and o_near:
                opt = subway_option("subway_car", "Subway, then accessible Uber/Lyft for the last leg",
                                    {x["stop"]: wheel_s(mm) for x, mm in o_near}, {s["stop"]: 0.0}, post=[leg], extra_cost=rr["fare"], post_min=car_min)
            else:
                opt = None
            if opt and (best is None or opt["minutes"] + opt["cost"] < best["minutes"] + best["cost"]):   # $1 ≈ 1 minute when picking the station
                best = opt
        return best

    door = options[-1]
    for first in (True, False):
        if (first and not o_near) or (not first and not d_near):
            h = hybrid(first)
            if h and h["cost"] < door["cost"] * 0.9:
                options.append(h)

    # effort, independence, warnings
    for o in options:
        wheels = [s for s in o["steps"] if s["mode"] == "wheel"]
        o["push_m"] = sum(s["metres"] for s in wheels)
        o["elevators"] = sum(1 for s in o["steps"] if s["mode"] in ("elevator", "transfer"))
        o["effort"] = o["push_m"] + 150 * o["elevators"]   # an elevator trip counted like 150 m of pushing, for sorting only
        reasons, level = [], "solo"
        if not o["possible"]:
            o["independence"] = {"level": "impossible", "label": "Not possible", "reasons": [o["why_not"]]}
            continue
        longest = max([s["metres"] for s in wheels], default=0)
        if longest > ch["help_m"]:
            level = "help"; reasons.append(f"A {longest:,} m push in a {ch['label']} — most people would want help or a ride for that leg")
        elif longest > ch["tiring_m"]:
            level = "risk"; reasons.append(f"A {longest:,} m push — doable, but tiring in a {ch['label']}")
        for s in o["stations"]:
            if s["ada"] == 2:
                level = "help" if level == "help" else "risk"; reasons.append(f"{s['name']} is accessible in one direction only ({s['ada_notes'] or 'see MTA'}) — check your direction")
            if s["out_now"]:
                x = s["out_now"][0]
                level = "help" if level == "help" else "risk"
                reasons.append(f"Elevator {x['equipment']} at {s['name']} is out right now ({x['reason'].lower()}, back {x['returns']}) — it serves: {x['serving']}")
            elif s["uptime"] is not None and s["uptime"] < 0.9:
                level = "help" if level == "help" else "risk"; reasons.append(f"Elevators at {s['name']} worked only {100 * s['uptime']:.0f}% of the time (Apr–Oct 2026)")
            if s["uptime"] is None:
                reasons.append(f"No elevator record for {s['name']} (it may be ramp-accessible)")
        if o["key"] == "car":
            reasons.append(f"Wait for an accessible car: {car['wait_min']:.0f} min typical, 1 in 10 over {car['wait_p90']:.0f} min")
        if not reasons:
            reasons.append("Every station on this route is fully accessible with elevators up 90% of the time or more")
        o["independence"] = {"level": level, "label": {"solo": "Can be done alone", "risk": "Alone, with a risk", "help": "Help likely needed"}[level], "reasons": reasons}

    keyf = {"time": lambda o: o["minutes"], "cost": lambda o: (o["cost"], o["minutes"]), "effort": lambda o: (o["effort"], o["minutes"])}.get(optimise) or (lambda o: o["minutes"])
    feasible = sorted([o for o in options if o["possible"]], key=keyf)
    for o in feasible:
        o["best_for"] = [k for k, f in (("time", lambda x: x["minutes"]), ("cost", lambda x: x["cost"]), ("effort", lambda x: x["effort"]))
                         if o is min(feasible, key=lambda x: (f(x), x["minutes"]))]
    return {
        "from": a, "to": b, "when": when, "when_label": PERIODS[when], "chair": chair, "chair_label": ch["label"], "optimise": optimise,
        "direct_km": round(direct / 1000, 1), "options": feasible + [o for o in options if not o["possible"]],
        "fares": FARES, "elevator_feed_time": net["feed_time"],
        "assumptions": [f"{ch['label'].capitalize()}: {ch['kmh']} km/h, stations considered within {ch['reach_m'] / 1000:g} km; sidewalk distance = straight line × {DETOUR}",
                        "3 minutes per elevator trip between street and platform; half a headway waiting for each train",
                        "Subway run times from the MTA weekday timetable; Uber/Lyft time and fare from September 2026 trips for the chosen time of day",
                        "Buses, platform gaps and street slope are not modelled"],
    }


def locate(text_or_point) -> dict:
    """Address string or {lat, lon, label} → point with taxi zone."""
    if isinstance(text_or_point, dict) and "lat" in text_or_point:
        z = engine.zone_of(text_or_point["lat"], text_or_point["lon"])
        if not z:
            return {"error": "that point is outside the five boroughs"}
        return {"lat": text_or_point["lat"], "lon": text_or_point["lon"], "label": text_or_point.get("label") or z["zone_name"], **z}
    g = engine.geocode(str(text_or_point))
    if "error" in g:
        return g
    return {"lat": g["lat"], "lon": g["lon"], "label": g["query"], "zone_id": g["zone_id"], "zone_name": g["zone_name"], "borough": g["borough"]}
