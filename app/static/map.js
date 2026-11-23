/* Wheelchair NYC — map-centric app. MapLibre GL + OpenFreeMap street map; everything else is local. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = n => "$" + Math.round(n).toLocaleString("en-US");
const num = n => Number(n).toLocaleString("en-US");
const NODATA = '<span class="nodata">no data</span>';
const pct = (x, d = 0) => x == null ? NODATA : (100 * x).toFixed(d) + "%";
const mins = x => x == null ? '<span class="nodata">too few rides</span>' : x.toFixed(1) + " min";
const say = t => { $("#status").textContent = ""; setTimeout(() => { $("#status").textContent = t; }, 30); };
const SP = new URLSearchParams(location.search);

const S = { boot: null, zi: new Map(), result: null, trip: null, tripSel: 0, layer: "reach", view: "score", a11y: false, three: false, crime: false,
            sel: null, sqls: [], runChecks: [], answerBudget: null, busy: false, sort: "rank", tripPts: {}, mapReady: false, hasGlyphs: true };
let map, markers = [], tripMarkers = [];

/* ---------- preferences ---------- */
const prefs = JSON.parse(localStorage.getItem("wnyc-prefs") || "{}");
if (SP.get("theme")) prefs.theme = SP.get("theme");
function applyPrefs() {
  document.documentElement.dataset.theme = prefs.theme === "ink" ? "ink" : "paper";
  document.documentElement.dataset.text = prefs.text === "large" ? "large" : "normal";
  $("#btn-theme").setAttribute("aria-pressed", prefs.theme === "ink");
  $("#btn-text").setAttribute("aria-pressed", prefs.text === "large");
  localStorage.setItem("wnyc-prefs", JSON.stringify(prefs));
}
$("#btn-theme").onclick = () => { prefs.theme = prefs.theme === "ink" ? "paper" : "ink"; applyPrefs(); };
$("#btn-text").onclick = () => { prefs.text = prefs.text === "large" ? "normal" : "large"; applyPrefs(); };
applyPrefs();

/* ---------- tabs ---------- */
const TABS = ["ask", "results", "trip", "layers", "about"];
function showTab(name, focus = false) {
  for (const t of TABS) {
    const on = t === name, b = $("#t-" + t);
    b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1; $("#p-" + t).hidden = !on;
    if (on) { b.querySelector(".dot")?.remove(); if (focus) b.focus(); }
  }
}
const flagTab = name => { const b = $("#t-" + name); if (b.getAttribute("aria-selected") !== "true" && !b.querySelector(".dot")) b.insertAdjacentHTML("beforeend", '<span class="dot" aria-hidden="true"></span><span class="vh"> (updated)</span>'); };
TABS.forEach((t, i) => {
  const b = $("#t-" + t);
  b.onclick = () => showTab(t);
  b.onkeydown = e => { const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key]; if (d) { e.preventDefault(); showTab(TABS[(i + d + TABS.length) % TABS.length], true); } };
});

/* ---------- colour ---------- */
const lerpPal = pal => t => {
  t = Math.max(0, Math.min(1, t)) * (pal.length - 1);
  const i = Math.min(pal.length - 2, Math.floor(t)), f = t - i, h = (s, o) => parseInt(s.slice(o, o + 2), 16);
  return "rgb(" + [1, 3, 5].map(o => Math.round(h(pal[i], o) + (h(pal[i + 1], o) - h(pal[i], o)) * f)).join(",") + ")";
};
const PAL = {
  good: lerpPal(["#c0392b", "#e67e22", "#f1c40f", "#9ccc3c", "#2e9e4f"]),          // everyday red → green
  seq: lerpPal(["#e8f1fa", "#9cc3e6", "#4a90d9", "#1f5fa8", "#0b2f66"]),            // neutral blue
  vir: lerpPal(["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c", "#28ae80", "#5ec962", "#addc30", "#fde725"]),
};
const scoreT = s => (s - 30) / 60;
const ROUTE = { 1: "#EE352E", 2: "#EE352E", 3: "#EE352E", 4: "#00933C", 5: "#00933C", 6: "#00933C", 7: "#B933AD", A: "#0039A6", C: "#0039A6", E: "#0039A6", B: "#FF6319", D: "#FF6319", F: "#FF6319", M: "#FF6319", G: "#6CBE45", J: "#996633", Z: "#996633", L: "#6d6e71", N: "#FCCC0A", Q: "#FCCC0A", R: "#FCCC0A", W: "#FCCC0A", S: "#5f6062", SIR: "#0039A6" };
const DARKTEXT = new Set(["N", "Q", "R", "W", "G", "B", "D", "F", "M"]);
const bullets = routes => (routes || []).map(r => `<span class="rt" style="background:${ROUTE[r] || "#333"};${DARKTEXT.has(r) ? "color:#14110f" : ""}">${esc(r)}</span>`).join("");

function md(text) {
  return esc(text).split(/\n{2,}/).map(b => b.trim()).filter(Boolean).map(b => {
    const lines = b.split("\n"), inline = s => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (lines.every(l => /^\s*[-*•]\s+/.test(l))) return "<ul>" + lines.map(l => "<li>" + inline(l.replace(/^\s*[-*•]\s+/, "")) + "</li>").join("") + "</ul>";
    return "<p>" + inline(lines.join(" ").replace(/^#+\s*/, "")) + "</p>";
  }).join("");
}

/* ---------- layers ---------- */
const LAYERS = [
  { key: "score", label: "Your score", sub: "How well each neighbourhood fits your budget and workplace", need: true },
  { key: "reach", label: "Accessible subway stations in reach", sub: "Inside the neighbourhood or within 800 m of where people live", get: z => z.ada_stations_800m, dir: "high", fmt: v => v + (v === 1 ? " station" : " stations"), steps: [0, 1, 2, 3] },
  { key: "rent", label: "Typical rent", sub: "Zillow, October 2026", get: z => z.rent_zori, dir: "low", fmt: money },
  { key: "near", label: "Homes near an accessible station", sub: "Share of apartments within 800 m", get: z => z.units_near_ada_share, dir: "high", fmt: v => pct(v) },
  { key: "elev", label: "Broken elevators at home", sub: "Tenant complaints per 1,000 apartments in elevator buildings, last 12 months", get: z => z.elev_complaints_per_1000_units, dir: "low", fmt: v => v.toFixed(1) + " per 1,000" },
  { key: "bus", label: "Bus stops", sub: "Per km² — every NYC bus has a ramp or lift", get: z => z.bus_stops_per_km2, dir: "high", fmt: v => v.toFixed(0) + " per km²" },
  { key: "wav", label: "Wait for an accessible Uber/Lyft", sub: "Typical minutes, September 2026", get: z => z.wav_wait_median_min, dir: "low", fmt: v => v.toFixed(1) + " min" },
  { key: "ped", label: "Pedestrians hit by cars", sub: "Injured or killed per km², last 12 months — busy areas rank high", get: z => z.ped_injuries_per_km2, dir: "low", fmt: v => v.toFixed(0) + " per km²" },
  { key: "hill", label: "Hills", sub: "Elevation range across homes — a rough proxy", get: z => z.elev_range_m, dir: "low", fmt: v => v.toFixed(0) + " m" },
  { key: "mob", label: "Neighbours with a mobility disability", sub: "CDC estimate, share of adults — context, not a rating", get: z => z.mobility_disability_pct, dir: null, fmt: v => v.toFixed(0) + "%" },
  { key: "crime", label: "Reported felonies", sub: "Per km², 2026 to date — reflects reporting and policing", get: z => z.felonies_per_km2, dir: "low", fmt: v => v.toFixed(0) + " per km²", crime: true },
];
const layerDef = k => LAYERS.find(l => l.key === k);
function layerStats(L) {            // percentile rank of each zone's value among zones with housing
  if (L._r) return L._r;
  const vals = [...S.zi.values()].filter(z => z.mf_units >= 1000 && L.get(z) != null).map(z => [z.zone_id, L.get(z)]).sort((a, b) => a[1] - b[1]);
  const rank = new Map(); vals.forEach(([id, v], i) => rank.set(id, { v, t: vals.length > 1 ? i / (vals.length - 1) : 0.5 }));
  if (L.steps) for (const r of rank.values()) r.t = Math.min(r.v, L.steps.length - 1) / (L.steps.length - 1);
  return (L._r = { rank, min: vals[0]?.[1], max: vals[vals.length - 1]?.[1] });
}
function renderLayerList() {
  $("#layer-list").innerHTML = LAYERS.filter(l => !l.crime || S.crime).map(l => `<label class="radio"><input type="radio" name="layer" value="${l.key}" ${l.key === S.layer ? "checked" : ""} ${l.need && !S.result ? "disabled" : ""}><span>${esc(l.label)}<small>${esc(l.need && !S.result ? "Ask a question first" : l.sub)}</small></span></label>`).join("");
}
$("#layer-list").addEventListener("change", e => { if (e.target.name === "layer") setLayer(e.target.value); });
function setLayer(k) { S.layer = k; if (k !== "score" && S.view === "deserts") toggleDeserts(false); paint(); renderLayerList(); say("Map coloured by " + layerDef(k).label.toLowerCase()); }

/* ---------- map ---------- */
const BLANK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#dfe8ee" } }] };
async function initMap() {
  let style = BLANK;
  // 1) the copy of the NYC street map saved on this machine (works offline)  2) OpenFreeMap online  3) plain background
  for (const url of ["/basemap/style.json", "https://tiles.openfreemap.org/styles/liberty"]) {
    try {
      const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 5000);
      const r = await fetch(url, { signal: ctl.signal }); clearTimeout(t);
      if (r.ok) { style = await r.json(); break; }
    } catch (e) { /* try the next source */ }
  }
  S.hasGlyphs = style !== BLANK;
  map = new maplibregl.Map({ container: "map", style, center: [-73.96, 40.705], zoom: 9.9, minZoom: 8.5, maxZoom: 18, maxPitch: 70, attributionControl: { compact: true } });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
  if (style === BLANK) { const n = $("#mode-note"); n.hidden = false; n.textContent = "The street map could not be loaded (offline?), so neighbourhoods are shown on a plain background."; }
  map.on("error", e => console.warn("map:", e && e.error && e.error.message));
  await new Promise(res => { if (map.isStyleLoaded()) res(); else map.once("style.load", res); });   // do not wait for tiles
  const firstSymbol = map.getStyle().layers.find(l => l.type === "symbol")?.id;
  const pat = (name, draw) => { const c = document.createElement("canvas"); c.width = c.height = 16; const x = c.getContext("2d"); draw(x); const d = x.getImageData(0, 0, 16, 16); map.addImage(name, { width: 16, height: 16, data: new Uint8Array(d.data.buffer) }); };
  pat("p-cross", x => { x.strokeStyle = "#5a554c"; x.lineWidth = 1.4; x.beginPath(); x.moveTo(0, 0); x.lineTo(16, 16); x.moveTo(16, 0); x.lineTo(0, 16); x.stroke(); });
  pat("p-stripe", x => { x.fillStyle = "#a3211a"; x.fillRect(0, 0, 16, 16); x.strokeStyle = "#ffe9e6"; x.lineWidth = 3; x.beginPath(); for (let i = -16; i < 32; i += 8) { x.moveTo(i, 16); x.lineTo(i + 16, 0); } x.stroke(); });
  pat("p-dot", x => { x.fillStyle = "#5a554c"; for (const [a, b] of [[4, 4], [12, 12]]) { x.beginPath(); x.arc(a, b, 1.6, 0, 7); x.fill(); } });

  map.addSource("zones", { type: "geojson", data: S.boot.zones });
  const add = (l, before = firstSymbol) => map.addLayer(l, before);
  add({ id: "z-fill", type: "fill", source: "zones", paint: { "fill-color": ["coalesce", ["get", "fill"], "rgba(0,0,0,0)"], "fill-opacity": ["coalesce", ["get", "op"], 0] } });
  add({ id: "z-pat", type: "fill", source: "zones", filter: ["!=", ["coalesce", ["get", "pat"], ""], ""], layout: { visibility: "none" }, paint: { "fill-pattern": ["get", "pat"] } });
  add({ id: "z-hit", type: "fill", source: "zones", paint: { "fill-color": "#000", "fill-opacity": 0.01 } });
  add({ id: "z-line", type: "line", source: "zones", paint: { "line-color": "#14110f", "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.3, 13, 1.2], "line-opacity": 0.45 } });
  // real buildings in 3D: restyle the basemap's extrusion layer (or add one) and keep it hidden until 3D is on
  S.bld = map.getLayer("building-3d") ? "building-3d" : null;
  if (!S.bld && map.getSource("openmaptiles")) { map.addLayer({ id: "wn-buildings", type: "fill-extrusion", source: "openmaptiles", "source-layer": "building", minzoom: 13, paint: {} }); S.bld = "wn-buildings"; }
  if (S.bld) {
    map.setLayerZoomRange(S.bld, 13, 24);
    map.setPaintProperty(S.bld, "fill-extrusion-height", ["coalesce", ["get", "render_height"], 6]);
    map.setPaintProperty(S.bld, "fill-extrusion-base", ["coalesce", ["get", "render_min_height"], 0]);
    map.setPaintProperty(S.bld, "fill-extrusion-color", ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 6], 0, "#f4efe6", 60, "#d9d2c5", 150, "#a8b4c4", 300, "#5f7391", 450, "#33415c"]);
    map.setPaintProperty(S.bld, "fill-extrusion-opacity", 0.92);
    map.setLayoutProperty(S.bld, "visibility", "none");
    map.moveLayer(S.bld, firstSymbol);   // above the neighbourhood colours, below the labels
  }
  add({ id: "z-top-o", type: "line", source: "zones", filter: [">", ["coalesce", ["get", "top"], 0], 0], paint: { "line-color": "#fffdf8", "line-width": 8 } });
  add({ id: "z-top-i", type: "line", source: "zones", filter: [">", ["coalesce", ["get", "top"], 0], 0], paint: { "line-color": "#14110f", "line-width": 3.5 } });
  add({ id: "z-sel", type: "line", source: "zones", filter: ["==", ["get", "zone_id"], -1], paint: { "line-color": "#d81b60", "line-width": 4.5 } });
  // trip route
  map.addSource("trip", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "trip-case", type: "line", source: "trip", paint: { "line-color": "#fffdf8", "line-width": 9 }, layout: { "line-cap": "round", "line-join": "round" } });
  map.addLayer({ id: "trip-subway", type: "line", source: "trip", filter: ["==", ["get", "mode"], "subway"], paint: { "line-color": ["get", "color"], "line-width": 6 }, layout: { "line-cap": "round", "line-join": "round" } });
  map.addLayer({ id: "trip-car", type: "line", source: "trip", filter: ["==", ["get", "mode"], "car"], paint: { "line-color": "#14110f", "line-width": 4.5 }, layout: { "line-cap": "round", "line-join": "round" } });
  map.addLayer({ id: "trip-wheel", type: "line", source: "trip", filter: ["==", ["get", "mode"], "wheel"], paint: { "line-color": "#d81b60", "line-width": 4.5, "line-dasharray": [1, 1] } });

  // stations and friends
  const st = S.boot.stations, byComplex = new Map(); st.forEach(s => byComplex.set(s.complex_id, s));
  const cls = u => u == null ? "none" : u >= 0.95 ? "ok" : u >= 0.85 ? "mid" : "bad";
  const fc = feats => ({ type: "FeatureCollection", features: feats });
  const pt = (lon, lat, props) => ({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: props });
  map.addSource("stations", { type: "geojson", data: fc(st.map(s => pt(s.lon, s.lat, { name: s.name, routes: s.routes, cls: cls(s.uptime), up: s.uptime == null ? "?" : Math.round(s.uptime * 100) + "%", ada: s.ada }))) });
  map.addLayer({ id: "stations", type: "circle", source: "stations", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 2.6, 12, 5, 15, 9],
    "circle-color": ["match", ["get", "cls"], "ok", "#0b6e4f", "mid", "#e69f00", "bad", "#b3261e", "#6b665e"],
    "circle-stroke-color": "#fffdf8", "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 9, 0.8, 13, 2] } });
  const outs = new Map(); for (const o of S.boot.outages) if (!o.upcoming) (outs.get(o.complex_id) || outs.set(o.complex_id, []).get(o.complex_id)).push(o);
  S.outs = outs;
  map.addSource("outages", { type: "geojson", data: fc([...outs].filter(([c]) => byComplex.has(c)).map(([c, list]) => { const s = byComplex.get(c); return pt(s.lon, s.lat, { name: s.name, n: list.length, text: list.map(o => `${o.equipment}: out since ${o.starts}, due back ${o.returns} (${o.reason.toLowerCase()}) — ${o.serving}`).join(" • ") }); })) });
  map.addLayer({ id: "outages", type: "circle", source: "outages", paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 6, 13, 13], "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#b3261e", "circle-stroke-width": 3 } });
  map.addSource("upgrades", { type: "geojson", data: fc(S.boot.upgrades.map(u => pt(u.lon, u.lat, { name: u.name, routes: u.routes, text: u.forecast ? "MTA forecast: accessible by " + u.forecast : "In procurement — no date yet" }))) });
  map.addLayer({ id: "upgrades", type: "circle", source: "upgrades", layout: { visibility: "none" }, paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 4, 13, 9], "circle-color": "#fffdf8", "circle-stroke-color": "#0b3d91", "circle-stroke-width": 3 } });
  if (S.hasGlyphs) {
    map.addSource("zone-pts", { type: "geojson", data: fc([]) });
    map.addLayer({ id: "z-lab", type: "symbol", source: "zone-pts", layout: { visibility: "none", "text-field": ["get", "lab"], "text-font": ["Noto Sans Bold"], "text-size": 13 }, paint: { "text-color": "#14110f", "text-halo-color": "#fffdf8", "text-halo-width": 2.2 } });
    map.addLayer({ id: "stations-lab", type: "symbol", source: "stations", minzoom: 12.5, layout: { visibility: "none", "text-field": ["get", "up"], "text-font": ["Noto Sans Bold"], "text-size": 11, "text-offset": [0, 1.3] }, paint: { "text-color": "#14110f", "text-halo-color": "#fffdf8", "text-halo-width": 2 } });
  }

  // interaction
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12, maxWidth: "320px" });
  const tip = (layer, html) => {
    map.on("mouseenter", layer, e => { map.getCanvas().style.cursor = "pointer"; popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map); });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; popup.remove(); });
    map.on("click", layer, e => { popup.setLngLat(e.features[0].geometry.coordinates).setHTML(html(e.features[0].properties)).addTo(map); e.originalEvent._handled = true; });
  };
  tip("stations", p => `<b>${esc(p.name)}</b> ${bullets(String(p.routes).split(" "))}<br>Elevators worked ${p.up === "?" ? "— no record" : esc(p.up) + " of the time"}${p.ada == 2 ? "<br>Accessible in one direction only" : ""}`);
  tip("outages", p => `<b>${esc(p.name)} — elevator out now</b><br>${esc(p.text)}`);
  tip("upgrades", p => `<b>${esc(p.name)}</b> ${bullets(String(p.routes).split(" "))}<br>${esc(p.text)}`);
  map.on("click", "z-hit", e => { if (e.originalEvent._handled) return; selectZone(e.features[0].properties.zone_id); });
  map.on("mousemove", "z-hit", () => { if (!map.getCanvas().style.cursor) map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "z-hit", () => { map.getCanvas().style.cursor = ""; });
  S.mapReady = true;
  paint();
}

function paint() {
  if (!S.mapReady) return;
  const r = S.result, L = layerDef(S.layer), deserts = S.view === "deserts", pal = S.a11y ? PAL.vir : PAL.good;
  const stats = L.get ? layerStats(L) : null, labs = [];
  for (const f of S.boot.zones.features) {
    const p = f.properties, id = p.zone_id, z = S.zi.get(id);
    let fill = null, op = 0, patn = "", h = 0, lab = "", top = 0;
    if (S.layer === "score" && r) {
      const st = r.zone_status[id] || { status: "no_rent_data" };
      if (deserts) {
        if (st.status === "desert") { fill = "#b3261e"; op = 0.62; patn = "p-stripe"; lab = "desert"; }
        else if (st.status === "qualifies") { fill = "#2e9e4f"; op = 0.3; }
      } else if (st.status === "qualifies") { const t = Math.max(0, Math.min(1, scoreT(st.score))); fill = pal(t); op = 0.68; h = 300 + 2600 * t; lab = String(Math.round(st.score)); top = st.rank <= 3 ? st.rank : 0; }
      else if (st.status === "desert" || st.status === "no_station") { fill = "#6b665e"; op = 0.22; patn = "p-cross"; }
      else if (st.status === "no_rent_data" || st.status === "little_housing") { patn = "p-dot"; }
    } else if (stats) {
      const v = stats.rank.get(id);
      if (v) { const good = L.dir === "low" ? 1 - v.t : v.t; fill = L.dir ? pal(good) : (S.a11y ? PAL.vir(v.t) : PAL.seq(v.t)); op = 0.66; h = 200 + 2400 * (L.dir ? good : v.t); lab = L.key === "reach" ? String(v.v) : ""; }
      else if (z && z.mf_units >= 1000) patn = "p-dot";
    }
    Object.assign(p, { fill, op, pat: patn, h, top });
    if (lab) labs.push({ type: "Feature", geometry: { type: "Point", coordinates: [p.label_lon, p.label_lat] }, properties: { lab } });
  }
  map.getSource("zones").setData(S.boot.zones);
  map.setLayoutProperty("z-pat", "visibility", S.a11y ? "visible" : "none");
  if (S.bld) map.setLayoutProperty(S.bld, "visibility", S.three ? "visible" : "none");
  if (S.hasGlyphs) { map.getSource("zone-pts").setData({ type: "FeatureCollection", features: labs }); map.setLayoutProperty("z-lab", "visibility", S.a11y ? "visible" : "none"); map.setLayoutProperty("stations-lab", "visibility", S.a11y ? "visible" : "none"); }
  map.setPaintProperty("stations", "circle-stroke-color", S.a11y ? "#14110f" : "#fffdf8");
  // markers
  markers.forEach(m => m.remove()); markers = [];
  if (r) {
    const w = document.createElement("div"); w.className = "work-mk"; w.textContent = "★"; w.title = "Your workplace: " + (r.work.label || "");
    markers.push(new maplibregl.Marker({ element: w }).setLngLat([r.work.lon, r.work.lat]).addTo(map));
    if (S.layer === "score" && !deserts) for (const t of r.top3) {
      const f = S.boot.zones.features.find(x => x.properties.zone_id === t.zone_id).properties, e = document.createElement("div");
      e.className = "badge-mk"; e.textContent = t.rank; e.title = `${t.rank}: ${t.zone_name}, score ${Math.round(t.score)}`; e.onclick = ev => { ev.stopPropagation(); selectZone(t.zone_id); };
      markers.push(new maplibregl.Marker({ element: e }).setLngLat([f.label_lon, f.label_lat]).addTo(map));
    }
  }
  renderLegend();
}

function renderLegend() {
  const L = layerDef(S.layer), deserts = S.view === "deserts", pal = S.a11y ? PAL.vir : PAL.good, grad = f => `linear-gradient(90deg, ${[0, .25, .5, .75, 1].map(f).join(",")})`;
  let h = `<p class="lt">${esc(deserts ? "Accessibility deserts" : L.label)}</p>`;
  if (deserts) h += `<ul><li><span class="sw sq" style="background:#b3261e"></span>Affordable, but no accessible station and slow accessible rides</li><li><span class="sw sq" style="background:#2e9e4f;opacity:.5"></span>Qualifies</li></ul>`;
  else if (S.layer === "score") h += `<div class="ramp" style="background:${grad(pal)}"></div><div class="ends"><span>30 — weaker fit</span><span>90 — stronger fit</span></div><ul><li><span class="sw sq" style="background:#6b665e;opacity:.45"></span>Affordable, but no accessible station${S.a11y ? " (crossed)" : ""}</li><li><span class="sw sq" style="background:#fffdf8"></span>Not coloured: over budget or no data${S.a11y ? " (dotted)" : ""}</li></ul>`;
  else { const s = layerStats(L), f = L.dir ? (L.dir === "low" ? t => pal(1 - t) : pal) : (S.a11y ? PAL.vir : PAL.seq); h += `<div class="ramp" style="background:${grad(f)}"></div><div class="ends"><span>${L.fmt(s.min)}</span><span>${L.dir ? (L.dir === "low" ? "more is worse →" : "more is better →") : ""}</span><span>${L.fmt(s.max)}</span></div><p style="margin-top:4px">${esc(L.sub)}. Colours rank neighbourhoods against each other.</p>`; }
  const ov = [];
  if ($("#ov-stations").checked) ov.push(`<li><span class="sw" style="background:#0b6e4f"></span><span><b>Subway station with elevators</b> (wheelchair-accessible). Green: its elevators worked 95% of the time or more</span></li><li><span class="sw" style="background:#e69f00"></span>Same, elevators worked 85–95%</li><li><span class="sw" style="background:#b3261e"></span>Same, elevators worked under 85%</li><li><span class="sw" style="background:#6b665e"></span>Accessible station with no elevator record</li><li class="small">Stations without elevators and bus stops are not drawn.</li>`);
  if ($("#ov-outages").checked) ov.push(`<li><span class="sw" style="background:#fff;border:3px solid #b3261e"></span>Red ring: an elevator at this station is out right now (as of ${S.boot.feed_time})</li>`);
  if ($("#ov-upgrades").checked) ov.push(`<li><span class="sw" style="background:#fff;border:3px solid #0b3d91"></span>Blue ring: station due to get elevators</li>`);
  if (S.result) ov.push(`<li><span style="color:#d81b60;font-size:1.1rem;line-height:1;width:16px;text-align:center">★</span>Your workplace</li>`);
  if (S.result && S.layer === "score" && !deserts) ov.push(`<li><span class="sw" style="background:#ffd400;border-width:2.5px"></span>Numbered circle and thick outline: your top three</li>`);
  if (S.trip) ov.push(`<li><span class="ln" style="background:#14110f"></span>Trip by car</li><li><span class="ln" style="background:repeating-linear-gradient(90deg,#d81b60 0 5px,transparent 5px 9px)"></span>Trip under your own power</li><li><span class="ln" style="background:#00933C"></span>Trip by subway (line colour)</li>`);
  if (S.a11y) ov.push(`<li>Numbers on the map give the exact value</li>`);
  $("#legend-body").innerHTML = `<h3>Neighbourhood colours</h3>` + h + (ov.length ? `<h3>Dots and marks</h3><ul>${ov.join("")}</ul>` : "");
}

const fitPad = () => ({ top: 90, left: 70, bottom: 70, right: innerWidth > 820 && $("#legend-toggle").getAttribute("aria-expanded") === "true" ? 350 : 80 });
$("#legend-toggle").onclick = e => {
  const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
  e.currentTarget.setAttribute("aria-expanded", open); $("#legend-body").hidden = !open;
  e.currentTarget.querySelector("span").textContent = open ? "Hide" : "Show"; prefs.legend = open ? "open" : "closed"; localStorage.setItem("wnyc-prefs", JSON.stringify(prefs));
};
if (prefs.legend === "closed") $("#legend-toggle").click();

/* ---------- zone card ---------- */
const streetUrl = (lat, lon) => `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${(+lat).toFixed(6)},${(+lon).toFixed(6)}`;
function alertsHtml(list) {
  if (!list || !list.length) return "";
  const head = { warn: "Heads-up", good: "Good news", info: "Coming up" };
  return `<ul class="alerts">${list.map(a => `<li class="${a.kind}"><b>${head[a.kind] || "Note"}</b>${esc(a.text)}</li>`).join("")}</ul>`;
}
function staticAlerts(z) {
  const a = [];
  if (z.upgrade_names) a.push({ kind: "good", text: "Becoming accessible nearby: " + z.upgrade_names + ". MTA forecasts; several earlier ones have slipped" });
  if (z.lottery_active) a.push({ kind: "good", text: `Open affordable-housing lottery now: ${num(z.lottery_active_units)} units, about ${z.lottery_active_mobility_units} set aside for mobility disabilities (${z.lottery_active_names})` });
  return a;
}
function selectZone(id, { fly = false } = {}) {
  id = +id; S.sel = id; const z = S.zi.get(id); if (!z) return;
  map.setFilter("z-sel", ["==", ["get", "zone_id"], id]);
  $$(".hl").forEach(e => e.classList.remove("hl")); $$(`[data-zone="${id}"]`).forEach(e => e.classList.add("hl"));
  const r = S.result, st = r ? (r.zone_status[id] || { status: "no_rent_data" }) : null, t = st && st.status === "qualifies" ? r.ranked[st.rank - 1] : null;
  let verdict = "";
  if (t) verdict = `<p><b>#${t.rank} of ${r.ranked.length}</b> · score <b>${t.score.toFixed(0)}</b>. ${esc(t.commute.text)}.</p><p><b>Biggest risk:</b> ${esc(t.risk)}.</p>`;
  else if (st) verdict = `<p>${esc({ over_budget: `Typical rent is over your budget of ${money(r.params.budget)}.`, no_rent_data: "No rent data here, so it is not ranked.", little_housing: "Very little housing here (park, airport or industrial area), so it is not ranked.", desert: "Accessibility desert: the rent fits, but there is no accessible subway station in reach and accessible rides are slow or too rare to measure.", no_station: "The rent fits, but there is no accessible subway station in reach." }[st.status])}</p>`;
  const row = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  const f = S.boot.zones.features.find(x => x.properties.zone_id === id).properties;
  const lat = t?.home_station?.lat ?? z.home_lat ?? f.label_lat, lon = t?.home_station?.lon ?? z.home_lon ?? f.label_lon;
  $("#zonecard").hidden = false;
  $("#zonecard").innerHTML = `<button type="button" class="close" aria-label="Close neighbourhood card">×</button>
    <h2>${esc(z.zone_name)}</h2><p class="small">${esc(z.borough)}${z.mf_units ? " · " + num(z.mf_units) + " apartments" : ""}</p>${verdict}
    <dl>${row("Typical rent", z.rent_zori ? money(z.rent_zori) : NODATA)}
      ${row("Accessible stations", z.ada_stations_800m ? z.ada_stations_800m + " in reach" : "none in reach")}
      ${row("Their elevators worked", pct(z.ada_elevator_uptime, 1))}
      ${row("Homes near one", pct(z.units_near_ada_share))}
      ${row("In elevator buildings", pct(z.elevator_units_share))}
      ${row("Broken-elevator complaints", z.elev_complaints_per_1000_units != null ? `${z.hpd_elev_buildings} buildings, 12 mo` : NODATA)}
      ${row("Bus stops", `${z.bus_stops} on ${z.bus_routes} routes`)}
      ${row("Accessible ride wait", mins(z.wav_wait_median_min))}
      ${row("Hills", z.elev_range_m != null ? z.elev_range_m.toFixed(0) + " m range" : NODATA)}
      ${row("Nearest hospital", z.hospital_nearest_km != null ? z.hospital_nearest_km + " km" : NODATA)}
      ${row("Accessible public toilets", z.restrooms_accessible)}
      ${row("Pedestrians hit, 12 mo", z.ped_injuries)}
      ${S.crime ? row("Reported felonies, 2026", `${num(z.felonies)} <span class="small">(reporting-dependent)</span>`) : ""}</dl>
    ${z.ada_station_names ? `<p class="small" style="margin-top:6px">Stations: ${esc(z.ada_station_names)}</p>` : ""}
    ${alertsHtml(t ? t.alerts : staticAlerts(z))}
    <p class="links"><a href="${streetUrl(lat, lon)}" target="_blank" rel="noopener">Street View here ↗<span class="vh"> (opens Google Maps in a new tab)</span></a>
      <button type="button" data-trip-from="${id}">${r ? "Trip from here to work" : "Plan a trip from here"}</button></p>`;
  $("#zonecard .close").onclick = () => { $("#zonecard").hidden = true; map.setFilter("z-sel", ["==", ["get", "zone_id"], -1]); S.sel = null; };
  if (fly) map.flyTo({ center: [f.label_lon, f.label_lat], zoom: Math.max(map.getZoom(), 12.2), essential: false });
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-trip-from]"), zb = e.target.closest("[data-zone-btn]");
  if (zb) selectZone(zb.dataset.zoneBtn, { fly: true });
  if (b) {
    const z = S.zi.get(+b.dataset.tripFrom), f = S.boot.zones.features.find(x => x.properties.zone_id === z.zone_id).properties;
    setTripPoint("from", { lat: z.home_lat ?? f.label_lat, lon: z.home_lon ?? f.label_lon, label: z.zone_name + " (where most homes are)" });
    showTab("trip"); if (S.tripPts.to || $("#trip-to").value.trim()) $("#trip-form").requestSubmit(); else $("#trip-to").focus();
  }
});

/* ---------- results ---------- */
const PARTS = [["housing", "Housing"], ["transit", "Subway"], ["street", "Ramps"], ["plan_b", "Plan B"], ["rent", "Rent room"]];
const commuteShort = t => ({ one_seat: ["yes", "One-seat ride"], transfer: ["", "1 transfer"], no_route: ["no", "No route"], no_work_station: ["no", "No station at work"], no_home_station: ["no", "No station"] }[t.commute.kind]);
function renderResults() {
  const r = S.result, c = r.counts;
  $("#results-empty").hidden = true; $("#results-body").hidden = false;
  $("#filter-line").innerHTML = `<b>${c.affordable}</b> neighbourhoods have a typical rent within ${money(r.params.budget)}. Only <b>${c.qualifying}</b> of them also have an accessible subway station.` + (r.notes.length ? ` <b>${esc(r.notes[0])}</b>` : "");
  $("#top3").innerHTML = r.top3.length ? r.top3.map(t => {
    const hs = t.home_station, cs = commuteShort(t), pb = t.plan_b_ride;
    return `<li class="card" data-zone="${t.zone_id}"><div class="card-head"><span class="n" aria-hidden="true">${t.rank}</span>
      <h3><button type="button" data-zone-btn="${t.zone_id}"><span class="vh">Number ${t.rank}: </span>${esc(t.zone_name)}</button><span class="small" style="display:block;font-weight:400">${esc(t.borough)} · ${t.dist_work_km} km from work</span></h3>
      <p class="sc">${t.score.toFixed(0)}<small>of 100</small></p></div>
      <div class="bar" aria-hidden="true">${PARTS.map(([k]) => `<i style="flex:${r.weights[k].max}"><u style="width:${100 * t.components[k] / r.weights[k].max}%"></u></i>`).join("")}</div>
      <p class="parts">${PARTS.map(([k, l]) => `<span>${l} <b>${t.components[k].toFixed(0)}</b>/${r.weights[k].max}</span>`).join("")}</p>
      <dl class="facts">
        <div><dt>Typical rent</dt><dd><span class="big">${money(t.rent)}</span><small>${money(t.rent_headroom)} under budget</small></dd></div>
        <div><dt>Accessible station</dt><dd>${hs ? bullets(hs.routes) + " " + esc(hs.name) : NODATA}<small>${hs ? `elevators worked ${hs.uptime == null ? "— no record" : pct(hs.uptime, 1)}` : ""}</small></dd></div>
        <div><dt>Ride to work</dt><dd><span class="pill ${cs[0]}">${cs[1]}</span><small>${t.commute.kind === "one_seat" ? bullets(t.commute.routes) + " to " + esc(t.commute.work_station.name) : esc(t.commute.text)}</small></dd></div>
        <div><dt>Plan B: Uber/Lyft to work</dt><dd>${pb ? `<span class="big">${money(pb.fare)}</span><small>${Math.round(pb.minutes)} min${pb.monthly_cost != null ? ` · about ${money(pb.monthly_cost)}/month if the elevator fails as often as it did` : ""}</small>` : '<span class="nodata">too few trips</span>'}</dd></div>
      </dl>
      <ul class="pm">${t.pluses.map(p => `<li><span class="ic" aria-hidden="true">+</span><span><span class="vh">Plus: </span>${esc(p)}</span></li>`).join("")}
        <li class="risk"><span class="ic" aria-hidden="true">!</span><span><span class="vh">Biggest risk: </span>${esc(t.risk)}</span></li></ul>
      ${alertsHtml(t.alerts)}
      <p class="links"><button type="button" data-trip-from="${t.zone_id}">Check this commute</button>${hs ? `<a href="${streetUrl(hs.lat, hs.lon)}" target="_blank" rel="noopener">Street View at the station ↗</a>` : ""}</p></li>`;
  }).join("") : `<li class="hint">No neighbourhood has a typical rent within ${money(r.params.budget)} and an accessible station in reach. Move the budget up to see where the map opens.</li>`;
  const key = { rank: t => t.rank, rent: t => t.rent, uptime: t => -(t.home_station?.uptime ?? -1), fare: t => t.plan_b_ride?.fare ?? 9999, dist: t => t.dist_work_km }[S.sort];
  const rows = [...r.ranked].sort((a, b) => key(a) - key(b) || a.rank - b.rank);
  $("#ranked").innerHTML = `<caption>${rows.length} neighbourhoods. Choose a name to see it on the map.</caption><thead><tr><th scope="col">#</th><th scope="col">Neighbourhood</th><th scope="col">Score</th><th scope="col">Rent</th><th scope="col">To work</th></tr></thead><tbody>${rows.map(t => { const cs = commuteShort(t); return `<tr data-zone="${t.zone_id}"><td class="num">${t.rank}</td><th scope="row"><button type="button" data-zone-btn="${t.zone_id}">${esc(t.zone_name)}</button><span class="sub">${esc(t.borough)}${t.alerts.some(a => a.kind === "warn") ? " · elevator out now" : ""}</span></th><td class="num"><b>${t.score.toFixed(0)}</b></td><td class="num">${money(t.rent)}</td><td><span class="pill ${cs[0]}">${cs[1]}</span><span class="sub">${t.plan_b_ride ? money(t.plan_b_ride.fare) + " by car" : ""}</span></td></tr>`; }).join("")}</tbody>`;
  const d = r.deserts;
  $("#deserts-line").innerHTML = d.length ? `<b>${d.length} neighbourhoods</b> with about <b>${num(r.counts.desert_units)} apartments</b> look affordable at ${money(r.params.budget)}, but have no accessible subway station in reach, and accessible Uber/Lyft is slower than the city’s typical ${r.wav_city_median_min.toFixed(1)} minutes or too rare to measure.` : `No accessibility deserts at ${money(r.params.budget)}.`;
  $("#deserts-table").innerHTML = `<thead><tr><th scope="col">Neighbourhood</th><th scope="col">Rent</th><th scope="col">Accessible ride wait</th></tr></thead><tbody>${d.map(z => `<tr data-zone="${z.zone_id}"><th scope="row"><button type="button" data-zone-btn="${z.zone_id}">${esc(z.zone_name)}</button><span class="sub">${esc(z.borough)} · ${num(z.mf_units)} apartments</span></th><td class="num">${money(z.rent)}</td><td class="num">${mins(z.wav_wait_median_min)}</td></tr>`).join("")}</tbody>`;
}
$("#sort").onchange = e => { S.sort = e.target.value; renderResults(); };
function renderMini() {
  const r = S.result, w = r.work;
  $("#answer-wrap").hidden = false; $("#ask-hint").hidden = true;
  $("#understood").innerHTML = `<span class="tag"><small>Budget</small>${money(r.params.budget)}</span><span class="tag"><small>Work</small>${esc(w.label || "")}</span><span class="tag"><small>Accessible stations at work</small>${w.stations.length || "none"}</span>`;
  $("#mini-top").innerHTML = r.top3.map(t => `<li><button type="button" data-zone-btn="${t.zone_id}"><span class="n" aria-hidden="true">${t.rank}</span><span><b>${esc(t.zone_name)}</b><span class="small">${esc(t.borough)} · ${money(t.rent)} · ${commuteShort(t)[1].toLowerCase()}</span></span><span class="sc">${t.score.toFixed(0)}</span></button></li>`).join("") || `<li class="hint">No neighbourhood matched this budget.</li>`;
}
function fitResult() {
  const r = S.result; if (!r || !S.mapReady) return;
  const b = new maplibregl.LngLatBounds([r.work.lon, r.work.lat], [r.work.lon, r.work.lat]);
  for (const t of r.top3) { const f = S.boot.zones.features.find(x => x.properties.zone_id === t.zone_id).properties; b.extend([f.label_lon, f.label_lat]); }
  map.fitBounds(b, { padding: fitPad(), maxZoom: 12.5, duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 900 });
}
function applyResult(data, { fromDial = false } = {}) {
  S.result = data;
  if (!fromDial) { S.layer = "score"; $("#budget").value = Math.max(1500, Math.min(8000, data.params.budget)); S.answerBudget = data.params.budget; }
  $("#dial-out").textContent = money(data.params.budget);
  renderMini(); renderResults(); renderLayerList(); renderAbout(); paint();
  if (!fromDial) {
    fitResult(); flagTab("results");
    setTripPoint("to", { lat: data.work.lat, lon: data.work.lon, label: data.work.label || "Work" });
    if (data.top3[0]) { const z = S.zi.get(data.top3[0].zone_id); setTripPoint("from", { lat: z.home_lat, lon: z.home_lon, label: z.zone_name + " (where most homes are)" }); }
  }
  if (S.sel) selectZone(S.sel);
}
let dialT = null, dialSeq = 0;
$("#budget").addEventListener("input", e => {
  const b = +e.target.value; $("#dial-out").textContent = money(b);
  clearTimeout(dialT); dialT = setTimeout(async () => {
    const r = S.result; if (!r) return; const seq = ++dialSeq;
    const res = await fetch("/api/score", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ budget: b, work_lat: r.params.work_lat, work_lon: r.params.work_lon, work_label: r.params.work_label }) });
    if (!res.ok || seq !== dialSeq) return;
    const d = await res.json(); applyResult(d, { fromDial: true });
    say(`${money(b)}: ${d.counts.qualifying} neighbourhoods qualify.` + (d.top3[0] ? ` First is ${d.top3[0].zone_name}.` : ""));
  }, 130);
});
function toggleDeserts(on) {
  S.view = on ? "deserts" : "score"; $("#deserts").setAttribute("aria-checked", on);
  $("#deserts-wrap").hidden = !on; $("#ranked-wrap").hidden = on;
  if (on) { S.layer = "score"; renderLayerList(); }
  paint(); if (on && S.mapReady) map.easeTo({ center: [-73.94, 40.71], zoom: 9.8 });
}
$("#deserts").onclick = e => { const on = e.currentTarget.getAttribute("aria-checked") !== "true"; toggleDeserts(on); say(on ? `Showing ${S.result.deserts.length} accessibility deserts.` : "Showing ranked neighbourhoods."); };

/* ---------- ask ---------- */
function addStep(ev) { const tr = $("#trace"); tr.querySelectorAll("li.active").forEach(li => li.remove()); const li = document.createElement("li"); li.className = ev.status || "done"; li.innerHTML = `<b>${esc(ev.label)}</b>${ev.detail ? ` <span>— ${esc(ev.detail)}</span>` : ""}`; tr.appendChild(li); }
async function ask(question, fresh = false) {
  if (S.busy) return; S.busy = true;
  const go = $("#go"); go.disabled = true; go.querySelector("span").textContent = "Working…";
  S.sqls = []; S.runChecks = []; let buf = "", got = false, mode = null;
  history.replaceState(null, "", "?q=" + encodeURIComponent(question));
  showTab("ask"); $("#answer-wrap").hidden = false; $("#ask-hint").hidden = true; $("#trace").innerHTML = ""; $("#trace-wrap").open = true; $("#trace-title").textContent = "Working on it…";
  $("#answer-text").innerHTML = ""; $("#answer-by").textContent = ""; $("#mode-note").hidden = true; $("#mini-top").innerHTML = ""; $("#understood").innerHTML = "";
  say("Working on your question.");
  const at = $("#answer-text");
  const handle = ev => {
    if (ev.type === "mode") { mode = ev; if (ev.mode === "offline") { const n = $("#mode-note"); n.hidden = false; n.textContent = ev.reason; } }
    else if (ev.type === "step") addStep(ev);
    else if (ev.type === "sql") { S.sqls.push(ev); addStep({ label: ev.by === "model" ? "Checked its own work with SQL" : "Ran a fixed check", detail: ev.purpose, status: ev.error ? "warn" : "done" }); }
    else if (ev.type === "check") { S.runChecks.push(ev); addStep({ label: ev.name, detail: ev.detail, status: ev.ok ? "done" : "warn" }); }
    else if (ev.type === "result") { got = true; applyResult(ev.data); }
    else if (ev.type === "trip") { S.trip = ev.data; S.tripSel = 0; renderTrip(); flagTab("trip"); }
    else if (ev.type === "map") { $("#png").innerHTML = `<img src="/api/map/${esc(ev.id)}.png" alt="Static map of the result: zones coloured by score, the top three outlined and numbered, the workplace as a star. The same information is in the Results tab."><p class="small">${ev.seen_by_model ? "This exact image was sent back to the model, which compared it with its own top three." : "No model was connected, so this image was rendered but not inspected."}</p>`; }
    else if (ev.type === "answer_delta") { buf += ev.text; at.classList.add("streaming"); at.innerHTML = md(buf); }
    else if (ev.type === "answer_reset") { buf = ""; at.innerHTML = ""; at.classList.remove("streaming"); }
    else if (ev.type === "answer") { at.classList.remove("streaming"); at.innerHTML = md(ev.text); $("#answer-by").textContent = ev.model ? `Written by ${ev.model} after running its own checks and looking at the rendered map.${mode && mode.cached_at ? ` Replayed from a run at ${mode.cached_at}.` : ""}` : "Written by fixed rules — no AI model was involved in this answer."; }
    else if (ev.type === "error") addStep({ label: "Something went wrong", detail: ev.message, status: "warn" });
    else if (ev.type === "done") { $("#trace-title").textContent = `What it did (${ev.seconds} s${ev.cached ? ", replayed" : ""})`; $("#trace-wrap").open = false; }
  };
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, fresh }) });
    if (!res.ok) throw new Error("server said " + res.status);
    const reader = res.body.getReader(), dec = new TextDecoder(); let tail = "";
    for (;;) { const { value, done } = await reader.read(); if (done) break; tail += dec.decode(value, { stream: true }); const lines = tail.split("\n"); tail = lines.pop(); for (const l of lines) if (l.trim()) handle(JSON.parse(l)); }
  } catch (e) { addStep({ label: "Could not reach the app server", detail: String(e.message || e), status: "warn" }); }
  S.busy = false; go.disabled = false; go.querySelector("span").textContent = "Find my neighbourhoods";
  $("#trace").querySelectorAll("li.active").forEach(li => li.remove()); renderAbout();
  const r = S.result;
  say(got && r ? (r.top3.length ? `Done. Top three: ${r.top3.map(t => `${t.zone_name}, score ${t.score.toFixed(0)}`).join("; ")}.` : "Done. No neighbourhood matched.") : "Done. " + at.textContent.slice(0, 200));
}
$("#ask-form").addEventListener("submit", e => { e.preventDefault(); const q = $("#q").value.trim(); if (q) ask(q, e.shiftKey); });
/* demo helper: Enter in an empty field fills its example; Enter again submits */
const fillExample = el => { el.value = el.dataset.example; el.dispatchEvent(new Event("input", { bubbles: true })); el.setSelectionRange(el.value.length, el.value.length); say("Example filled in: " + el.value); };
$$("[data-example]").forEach(el => el.addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  if (!el.value.trim()) { e.preventDefault(); fillExample(el); if (el.id === "trip-from" && !$("#trip-to").value.trim()) $("#trip-to").focus(); }
  else if (el.id === "q") { e.preventDefault(); $("#ask-form").requestSubmit(); }
}));
$$("[data-fill]").forEach(b => b.onclick = () => { const els = b.dataset.fill.split(" ").map(id => $("#" + id)); els.forEach(el => { if (el.id.startsWith("trip-")) delete S.tripPts[el.id.slice(5)]; fillExample(el); }); els[els.length - 1].focus(); });
$$(".chip").forEach(c => c.onclick = () => { $("#q").value = c.dataset.q; ask(c.dataset.q); });

/* ---------- trip ---------- */
function setTripPoint(which, p) { S.tripPts[which] = p; $("#trip-" + which).value = p.label; }
const tripVal = which => { const v = $("#trip-" + which).value.trim(), p = S.tripPts[which]; return p && p.label === v ? p : v; };
$("#trip-form").addEventListener("submit", async e => {
  e.preventDefault();
  const b = $("#trip-go"); b.disabled = true; b.querySelector("span").textContent = "Checking…"; say("Checking the trip.");
  const body = { origin: tripVal("from"), destination: tripVal("to"), when: $("#trip-when").value, chair: $('input[name="chair"]:checked').value, optimise: $('input[name="opt"]:checked').value };
  try {
    const res = await fetch("/api/trip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }), d = await res.json();
    if (!res.ok) { $("#trip-out").innerHTML = `<p class="note">${esc(d.error || "Could not plan that trip.")}${d.hubs ? " Try a full address, or: " + esc(d.hubs.join(", ")) + "." : ""}</p>`; say(d.error || "Could not plan that trip."); }
    else { S.trip = d; S.tripSel = 0; renderTrip(); const o = d.options[0]; say(o && o.possible ? `${d.options.filter(x => x.possible).length} ways to make this trip. First: ${o.title}, about ${o.minutes} minutes, ${money(o.cost)}.` : "No workable way found for this trip."); }
  } catch (err) { $("#trip-out").innerHTML = `<p class="note">Could not reach the app server.</p>`; }
  b.disabled = false; b.querySelector("span").textContent = "Check this trip";
});
$$('input[name="opt"]').forEach(i => i.onchange = () => { if (S.trip) $("#trip-form").requestSubmit(); });
function stepHtml(s) {
  if (s.mode === "wheel") return `<li><b>${esc(s.verb || "Wheel")} ${num(s.metres)} m</b> — about ${Math.round(s.minutes)} min<small>${esc(s.from)} → ${esc(s.to)}</small></li>`;
  if (s.mode === "elevator") return `<li><b>Elevator at ${esc(s.at)}</b> — ${s.minutes} min${s.wait_train_min != null ? `, then about ${s.wait_train_min} min for a train` : ""}</li>`;
  if (s.mode === "subway") return `<li><b>${bullets([s.route])} train, ${s.stops} stop${s.stops > 1 ? "s" : ""}</b> — ${Math.round(s.minutes)} min<small>${esc(s.from)} → ${esc(s.to)}</small></li>`;
  if (s.mode === "transfer") return `<li><b>Change at ${esc(s.at)}</b> to the ${bullets([s.to_route])} — about ${Math.round(s.minutes)} min with elevators and waiting${s.uptime != null ? `<small>its elevators worked ${pct(s.uptime)} of the time</small>` : ""}</li>`;
  return `<li><b>Accessible Uber/Lyft</b> — ${Math.round(s.wait_min)} min wait + ${Math.round(s.minutes)} min ride, ${money(s.fare)}<small>${esc(s.from)} → ${esc(s.to)} · ${esc(s.basis)} · 1 in 10 waits over ${Math.round(s.wait_p90)} min</small></li>`;
}
function renderTrip() {
  const d = S.trip; if (!d) return;
  if (!S.tripPts.from || S.tripPts.from.label !== d.from.label) setTripPoint("from", d.from);
  if (!S.tripPts.to || S.tripPts.to.label !== d.to.label) setTripPoint("to", d.to);
  const best = { time: "Fastest", cost: "Cheapest", effort: "Least effort" };
  $("#trip-out").innerHTML = `<h2 class="h">${esc(d.from.label)} → ${esc(d.to.label)}</h2><p class="small">${d.direct_km} km in a straight line · ${esc(d.when_label)} · ${esc(d.chair_label)}</p>` + d.options.map((o, i) => o.possible ? `
    <article class="opt ${i === S.tripSel ? "sel" : ""}" data-card="${i}"><h3><button type="button" data-opt="${i}" aria-pressed="${i === S.tripSel}">${esc(o.title)}</button></h3>
      <p class="onmap" aria-hidden="true"></p>
      <p class="nums"><b>${o.minutes} min</b><b>${o.cost ? money(o.cost) : "free"}</b><span>${o.cost_reduced != null ? `(${money(o.cost_reduced)} with Reduced-Fare)` : ""} ${o.push_m ? "· " + num(o.push_m) + " m under your own power" : "· door to door"}</span></p>
      <p><span class="badge ${o.independence.level}">${esc(o.independence.label)}</span>${(o.best_for || []).map(b => `<span class="badge best">${best[b]}</span>`).join("")}${o.elevator_reliability != null && o.stations.length ? `<span class="small"> every elevator working: ${pct(o.elevator_reliability)} of the time</span>` : ""}</p>
      <ol class="steps">${o.steps.map(stepHtml).join("")}</ol>
      <ul class="why">${o.independence.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul></article>` : `
    <article class="opt no"><h3>${esc(o.title)}</h3><p><span class="badge impossible">Not possible</span></p><p class="small">${esc(o.why_not)}</p></article>`).join("") +
    `<details class="fold" style="margin-top:12px"><summary>What is assumed</summary><ul class="bul">${d.assumptions.map(a => `<li>${esc(a)}</li>`).join("")}<li>${esc(d.paths_from || "")} Black line: car. Dashed pink: under your own power. Coloured: subway.</li><li>Subway fare ${money(d.fares.subway)}, ${money(d.fares.subway_reduced)} with Reduced-Fare; rides are capped at ${money(d.fares.weekly_cap)} a week. Elevator status as of ${esc(d.elevator_feed_time)}.</li></ul></details>`;
  drawTrip();
}
// the whole card switches the route on the map (the title is the keyboard target); no re-render, so nothing jumps
function selectOpt(i) {
  S.tripSel = i;
  $$("#trip-out [data-card]").forEach(c => { const on = +c.dataset.card === i; c.classList.toggle("sel", on); c.querySelector("[data-opt]").setAttribute("aria-pressed", on); });
  drawTrip(); const o = S.trip.options[i]; say(`Showing on the map: ${o.title}, about ${o.minutes} minutes.`);
}
$("#trip-out").addEventListener("click", e => {
  const card = e.target.closest("[data-card]");
  if (!card || e.target.closest("a, summary, details") || String(getSelection()).length) return;
  selectOpt(+card.dataset.card);
});
function drawTrip() {
  if (!S.mapReady) return;
  tripMarkers.forEach(m => m.remove()); tripMarkers = [];
  const d = S.trip, o = d && d.options[S.tripSel], feats = [];
  if (o && o.possible) {
    const A = [d.from.lon, d.from.lat], B = [d.to.lon, d.to.lat], ll = p => [p[1], p[0]];
    let cur = A; const line = (mode, coords, color) => feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: { mode, color: color || "#14110f" } });
    for (const s of o.steps) {
      const road = s.path && s.path.length > 1 ? s.path.map(ll) : (s.from_pt && s.to_pt ? [ll(s.from_pt), ll(s.to_pt)] : null);
      if (s.mode === "car") { line("car", road); cur = road[road.length - 1]; }
      else if (s.mode === "wheel" && road) { line("wheel", road); cur = road[road.length - 1]; }
      else if (s.mode === "subway") { const path = (s.path && s.path.length > 1 ? s.path : [s.from_pt, s.to_pt]).map(ll); line("subway", path, ROUTE[s.route] || "#333"); cur = path[path.length - 1]; }
    }
    for (const [p, txt] of [[A, "A"], [B, "B"]]) { const e = document.createElement("div"); e.className = "pin-mk"; e.innerHTML = `<span>${txt}</span>`; tripMarkers.push(new maplibregl.Marker({ element: e }).setLngLat(p).addTo(map)); }
    const b = new maplibregl.LngLatBounds(A, A); b.extend(B); feats.forEach(f => f.geometry.coordinates.forEach(c => b.extend(c)));
    map.fitBounds(b, { padding: fitPad(), maxZoom: 14, duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 800 });
  }
  map.getSource("trip").setData({ type: "FeatureCollection", features: feats });
  renderLegend();
}

/* ---------- about ---------- */
function renderAbout() {
  const r = S.result, W = (r && r.weights) || S.boot.weights;
  $("#weights").innerHTML = `<ul class="wts">${Object.values(W).map(w => `<li><span class="w">${w.max}</span><span><b>${esc(w.label)}</b><br>${esc(w.how)}</span></li>`).join("")}</ul><p class="small" style="margin-top:8px">Hard filters first: rent within budget, at least 1,000 apartments, an accessible station in reach. Missing data scores 0 for that part and is labelled. The extra layers (bus stops, elevator complaints, hills, crime…) are context and never change the score.</p>`;
  const blocks = S.sqls.map(q => `<div class="sqlblock"><h3>${esc(q.purpose)} <span class="small">— ${q.by === "model" ? "written by the model" : "fixed check"}</span></h3><pre><code>${esc(q.sql)}</code></pre>${q.error ? `<p><b>Error:</b> ${esc(q.error)}</p>` : q.columns ? `<table class="mini"><thead><tr>${q.columns.map(c => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead><tbody>${q.rows.map(row => `<tr>${row.map(v => `<td>${v == null ? "no data" : esc(typeof v === "number" && !Number.isInteger(v) ? +v.toFixed(4) : v)}</td>`).join("")}</tr>`).join("")}</tbody></table>` : ""}</div>`);
  if (r) blocks.push(`<div class="sqlblock"><h3>How the ranking is computed</h3><pre><code>${esc(r.method_sql)}</code></pre></div>`);
  $("#sqls").innerHTML = blocks.join("") || `<p class="small">Appears after a question.</p>`; $("#sql-count").textContent = S.sqls.length ? `(${S.sqls.length})` : "";
  const all = [...S.runChecks.map(c => ({ name: c.name, value: "", detail: c.detail, ok: c.ok })), ...S.boot.checks];
  $("#checks").innerHTML = `<ul class="checks">${all.map(c => `<li><b>${c.ok ? "OK" : "!!"}</b><div><b>${esc(c.name)}${c.value ? ": " + esc(c.value) : ""}</b> <span>${esc(c.detail)}</span></div></li>`).join("")}</ul>`;
  $("#check-count").textContent = `(${all.filter(c => c.ok).length} of ${all.length} OK)`;
  $("#feed-time").textContent = S.boot.feed_time;
}

/* ---------- switches ---------- */
function set3d(on) {
  S.three = on; $("#sw-3d").setAttribute("aria-checked", on); $("#q-3d").setAttribute("aria-pressed", on); paint();
  if (S.mapReady) {
    const quick = matchMedia("(prefers-reduced-motion: reduce)").matches, r = S.result;
    // buildings only exist in the map data from street level, so fly down to the workplace (or Midtown) if we are zoomed out
    const far = map.getZoom() < 14.2, target = far ? { center: r ? [r.work.lon, r.work.lat] : [-73.9857, 40.7484], zoom: 15.4 } : {};
    map.easeTo(on ? { ...target, pitch: 62, bearing: -20, duration: quick ? 0 : 1600 } : { pitch: 0, bearing: 0, duration: quick ? 0 : 900 });
  }
  say(on ? "3D buildings on. Zoom in to street level to see building heights; neighbourhood colours stay on the ground." : "3D buildings off.");
}
function setA11y(on) { S.a11y = on; $("#sw-a11y").setAttribute("aria-checked", on); $("#q-a11y").setAttribute("aria-pressed", on); paint(); say(on ? "Accessible colours on: colour-blind-safe palette, patterns and numbers." : "Standard colours."); }
$("#sw-3d").onclick = $("#q-3d").onclick = () => set3d(!S.three);
$("#sw-a11y").onclick = $("#q-a11y").onclick = () => setA11y(!S.a11y);
$("#q-fit").onclick = () => { if (S.three) set3d(false); map.easeTo({ center: [-73.96, 40.705], zoom: 9.9, pitch: 0, bearing: 0 }); };
$("#sw-crime").onclick = e => { S.crime = !S.crime; e.currentTarget.setAttribute("aria-checked", S.crime); $("#crime-note").hidden = !S.crime; if (!S.crime && S.layer === "crime") S.layer = S.result ? "score" : "reach"; renderLayerList(); paint(); if (S.sel) selectZone(S.sel); };
for (const [id, layers] of [["ov-stations", ["stations", "stations-lab"]], ["ov-outages", ["outages"]], ["ov-upgrades", ["upgrades"]]])
  $("#" + id).onchange = e => { layers.forEach(l => { if (map.getLayer(l)) map.setLayoutProperty(l, "visibility", e.target.checked && (l !== "stations-lab" || S.a11y) ? "visible" : "none"); }); renderLegend(); };

/* ---------- boot ---------- */
fetch("/api/bootstrap").then(r => r.json()).then(async b => {
  S.boot = b; b.zone_info.forEach(z => S.zi.set(z.zone_id, z));
  $("#trip-when").innerHTML = Object.entries(b.periods).map(([k, v]) => `<option value="${k}">${esc(v[0].toUpperCase() + v.slice(1))}</option>`).join("");
  if (!b.live) { const n = $("#mode-note"); n.hidden = false; n.textContent = "No Claude API key found, so answers use fixed rules over the same data. Set ANTHROPIC_API_KEY and restart to let the model plan, check and read the map."; }
  renderLayerList(); renderAbout();
  await initMap();
  if (SP.get("a11y")) setA11y(true);
  const q0 = SP.get("q");
  if (q0) { $("#q").value = q0; await ask(q0); }
  if (SP.get("layer") && layerDef(SP.get("layer"))) { if (SP.get("layer") === "crime") $("#sw-crime").click(); setLayer(SP.get("layer")); }
  if (SP.get("view") === "deserts" && S.result) toggleDeserts(true);
  if (SP.get("lift") || SP.get("3d")) set3d(true);
  if (SP.get("zone")) selectZone(SP.get("zone"), { fly: true });
  if (SP.get("trip")) { const [a, c] = SP.get("trip").split("|"); if (a) { $("#trip-from").value = a; delete S.tripPts.from; } if (c) { $("#trip-to").value = c; delete S.tripPts.to; } showTab("trip"); $("#trip-form").requestSubmit(); }
  else if (SP.get("tab")) showTab(SP.get("tab"));
}).catch(err => { console.error(err); $("#ask-hint").textContent = "Could not load the data. Is the server running, and has app/build_data.py been run?"; });
})();
