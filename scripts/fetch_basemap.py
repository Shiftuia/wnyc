"""Download the street map for New York City so the app works without internet.

Saves into data/basemap/: the map style (rewritten to load everything from this app), vector tiles for the NYC
bounding box at zooms 6–14 (the data's maximum; closer zooms reuse z14), label fonts and icons.
Source: OpenFreeMap (free, keyless, self-hosting encouraged) — © OpenStreetMap contributors, OpenMapTiles.

Run:  .venv/bin/python scripts/fetch_basemap.py        (~1,300 small files, a minute or two)
"""
from __future__ import annotations

import json
import math
import ssl
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "basemap"
BASE = "https://tiles.openfreemap.org"
BBOX = (-74.32, 40.46, -73.64, 40.96)     # NYC with a margin (west, south, east, north)
ZOOMS = range(6, 15)
FONTS = ["Noto Sans Regular", "Noto Sans Bold", "Noto Sans Italic"]
RANGES = ["0-255", "256-511", "8192-8447"]   # Latin, Latin Extended, general punctuation
CTX = ssl.create_default_context(cafile=certifi.where())


def get(url: str, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size > 0:
        return "have"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wheelchair-nyc-demo/1.0"})
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            body = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(body)
        tmp.rename(dest)
        return "ok"
    except urllib.error.HTTPError as e:
        return f"http{e.code}"
    except Exception as e:
        return type(e).__name__


def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    style = json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/styles/liberty", headers={"User-Agent": "wheelchair-nyc-demo/1.0"}), timeout=25, context=CTX).read())
    tj = json.loads(urllib.request.urlopen(urllib.request.Request(style["sources"]["openmaptiles"]["url"], headers={"User-Agent": "wheelchair-nyc-demo/1.0"}), timeout=25, context=CTX).read())
    template = tj["tiles"][0]
    jobs: list[tuple[str, Path]] = []
    for z in ZOOMS:
        x0, y1 = tile_xy(BBOX[0], BBOX[1], z)
        x1, y0 = tile_xy(BBOX[2], BBOX[3], z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                jobs.append((template.format(z=z, x=x, y=y), OUT / "tiles" / str(z) / str(x) / f"{y}.pbf"))
    n_tiles = len(jobs)
    for f in FONTS:
        for r in RANGES:
            jobs.append((f"{BASE}/fonts/{urllib.parse.quote(f)}/{r}.pbf", OUT / "fonts" / f / f"{r}.pbf"))
    sprite = style["sprite"]
    for ext in (".json", ".png", "@2x.json", "@2x.png"):
        jobs.append((sprite + ext, OUT / "sprite" / f"ofm{ext}"))
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda j: get(*j), jobs))
    counts: dict[str, int] = {}
    for r in res:
        counts[r] = counts.get(r, 0) + 1

    # local style: everything served by this app; {ORIGIN} is filled in per request by the server
    style["sources"].pop("ne2_shaded", None)
    style["layers"] = [l for l in style["layers"] if l.get("source") != "ne2_shaded"]
    style["sources"]["openmaptiles"] = {"type": "vector", "tiles": ["{ORIGIN}/basemap/tiles/{z}/{x}/{y}.pbf"], "minzoom": 0, "maxzoom": 14,
                                        "attribution": tj.get("attribution") or "© OpenStreetMap contributors, OpenMapTiles, OpenFreeMap"}
    style["glyphs"] = "{ORIGIN}/basemap/fonts/{fontstack}/{range}.pbf"
    style["sprite"] = "{ORIGIN}/basemap/sprite/ofm"
    (OUT / "style.json").write_text(json.dumps(style))
    (OUT / "upstream.json").write_text(json.dumps({"tiles": template, "fonts": BASE + "/fonts/{fontstack}/{range}.pbf", "sprite": sprite}))
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"tiles requested: {n_tiles}, all files: {len(jobs)}, results: {counts}, on disk: {size / 1e6:.0f} MB in {OUT.relative_to(ROOT)}")
    bad = sum(v for k, v in counts.items() if k not in ("ok", "have", "http404", "http204"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
