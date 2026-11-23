/* Wheelchair NYC front end — no framework, no external requests. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, html = "") => { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); e.innerHTML = html; return e; };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = n => "$" + Math.round(n).toLocaleString("en-US");
const NODATA = '<span class="nodata">no data</span>';
const pct = (x, d = 0) => x == null ? NODATA : (100 * x).toFixed(d) + "%";
const mins = x => x == null ? '<span class="nodata">too few rides</span>' : x.toFixed(1) + " min";

const S = { boot: null, result: null, view: "score", sort: { key: "rank", dir: 1 }, sqls: [], runChecks: [], answerBudget: null, busy: false, sel: null };

/* ---------- preferences ---------- */
const prefs = JSON.parse(localStorage.getItem("wnyc-prefs") || "{}");
if (prefs.theme == null && matchMedia("(prefers-color-scheme: dark)").matches) prefs.theme = "ink";
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

/* ---------- colour ---------- */
const VIR = ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c", "#28ae80", "#5ec962", "#addc30", "#fde725"];
function viridis(t) {
  t = Math.max(0, Math.min(1, t)) * (VIR.length - 1);
  const i = Math.min(VIR.length - 2, Math.floor(t)), f = t - i;
  const a = VIR[i], b = VIR[i + 1], h = (s, o) => parseInt(s.slice(o, o + 2), 16);
  return "rgb(" + [1, 3, 5].map(o => Math.round(h(a, o) + (h(b, o) - h(a, o)) * f)).join(",") + ")";
}
const scoreT = s => (s - 30) / 60;   // colour domain: 30 → 90+, same as the PNG the model sees
const ROUTE = { 1: "#EE352E", 2: "#EE352E", 3: "#EE352E", 4: "#00933C", 5: "#00933C", 6: "#00933C", 7: "#B933AD", A: "#0039A6", C: "#0039A6", E: "#0039A6", B: "#FF6319", D: "#FF6319", F: "#FF6319", M: "#FF6319", G: "#6CBE45", J: "#996633", Z: "#996633", L: "#6d6e71", N: "#FCCC0A", Q: "#FCCC0A", R: "#FCCC0A", W: "#FCCC0A", S: "#5f6062", SIR: "#0039A6" };
const DARKTEXT = new Set(["N", "Q", "R", "W", "G", "B", "D", "F", "M"]);
const bullets = routes => (routes || []).map(r => `<span class="rt" style="background:${ROUTE[r] || "#333"};${DARKTEXT.has(r) ? "color:#14110f" : ""}">${esc(r)}</span>`).join("");
const routeWords = routes => (routes || []).join(", ");

/* ---------- tiny markdown (escaped first) ---------- */
function md(text) {
  const blocks = esc(text).split(/\n{2,}/).map(b => b.trim()).filter(Boolean);
  return blocks.map(b => {
    const lines = b.split("\n");
    const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (lines.every(l => /^\s*[-*•]\s+/.test(l))) return "<ul>" + lines.map(l => "<li>" + inline(l.replace(/^\s*[-*•]\s+/, "")) + "</li>").join("") + "</ul>";
    return "<p>" + inline(lines.join(" ").replace(/^#+\s*/, "")) + "</p>";
  }).join("");
}

/* ---------- map ---------- */
const K = Math.cos(40.7 * Math.PI / 180), SC = 1000, LON0 = -74.27, LAT0 = 40.925;
const px = (lon, lat) => [(lon - LON0) * K * SC, (LAT0 - lat) * SC];
const FULL = { x: 0, y: 0, w: (0.585) * K * SC, h: 0.44 * SC };
const M = { svg: null, vb: { ...FULL }, zones: new Map(), gLift: null, lift: false, gTop: null, gStn: null, gBadge: null, gWork: null };
const NS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs = {}) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

function geomPath(g) {
  const polys = g.type === "MultiPolygon" ? g.coordinates : [g.coordinates];
  let d = "";
  for (const poly of polys) for (const ring of poly) {
    d += ring.map((c, i) => { const [x, y] = px(c[0], c[1]); return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); }).join("") + "Z";
  }
  return d;
}
function buildMap() {
  const svg = sv("svg", { "aria-hidden": "true", focusable: "false", preserveAspectRatio: "xMidYMid meet" });
  svg.innerHTML = `<defs>
    <pattern id="p-x" width="6" height="6" patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#f3ede0"/><path d="M0 0L6 6M6 0L0 6" stroke="#8a8375" stroke-width=".7"/></pattern>
    <pattern id="p-dot" width="5" height="5" patternUnits="userSpaceOnUse"><rect width="5" height="5" fill="#cfc9bc"/><circle cx="2.5" cy="2.5" r=".9" fill="#fffdf8"/></pattern>
    <pattern id="p-desert" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#a3211a"/><rect width="2" height="6" fill="#ffd9d4"/></pattern>
  </defs>`;
  const gZ = sv("g");
  for (const f of S.boot.zones.features) {
    const p = sv("path", { d: geomPath(f.geometry), class: "z", "fill-rule": "evenodd", fill: "#efe9db" });
    p.dataset.id = f.properties.zone_id;
    gZ.appendChild(p);
    M.zones.set(f.properties.zone_id, { path: p, props: f.properties, pt: px(f.properties.label_lon, f.properties.label_lat) });
  }
  M.gLift = sv("g"); M.gTop = sv("g"); M.gStn = sv("g"); M.gBadge = sv("g"); M.gWork = sv("g");
  svg.append(gZ, M.gLift, M.gTop, M.gStn, M.gWork, M.gBadge);
  for (const s of S.boot.stations) {
    const [x, y] = px(s.lon, s.lat), u = s.uptime;
    const g = sv("g", { class: "mk", "data-x": x, "data-y": y });
    let m;
    if (u == null) m = sv("path", { d: "M0 -3.4L3.4 0L0 3.4L-3.4 0Z", fill: "#5f5a52" });
    else if (u >= 0.95) m = sv("circle", { r: 2.6, fill: "#0b6e4f" });
    else if (u >= 0.85) m = sv("path", { d: "M0 -3.4L3.3 2.6L-3.3 2.6Z", fill: "#e69f00" });
    else m = sv("rect", { x: -2.7, y: -2.7, width: 5.4, height: 5.4, fill: "#a3211a" });
    m.setAttribute("class", "stn"); g.appendChild(m); M.gStn.appendChild(g);
  }
  M.svg = svg; $("#map").appendChild(svg);
  setView(FULL);

  for (const g of [gZ, M.gLift]) {
    g.addEventListener("pointerover", e => { const id = +e.target.dataset.id; if (id) inspect(id); });
    g.addEventListener("click", e => { const id = +e.target.dataset.id; if (id) select(id, { fromMap: true }); });
  }
  $("#lift").onclick = e => { M.lift = !M.lift; e.currentTarget.setAttribute("aria-pressed", M.lift); paintMap(); say(M.lift ? "3D view on: taller zones score higher. The table has the exact numbers." : "3D view off."); };
  const mapEl = $("#map");
  let drag = null;
  mapEl.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY, vb: { ...M.vb }, moved: false }; });
  window.addEventListener("pointermove", e => {
    if (!drag) return;
    const r = mapEl.getBoundingClientRect(), f = Math.max(M.vb.w / r.width, M.vb.h / r.height);
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; mapEl.style.cursor = "grabbing"; setView({ ...drag.vb, x: drag.vb.x - dx * f, y: drag.vb.y - dy * f }); }
  });
  window.addEventListener("pointerup", () => { drag = null; mapEl.style.cursor = ""; });
  mapEl.addEventListener("wheel", e => { if (!e.ctrlKey && !e.metaKey) return; e.preventDefault(); zoom(e.deltaY < 0 ? 0.8 : 1.25); }, { passive: false });
  mapEl.addEventListener("keydown", e => {
    const step = M.vb.w * 0.15, k = e.key;
    const act = { ArrowLeft: () => setView({ ...M.vb, x: M.vb.x - step }), ArrowRight: () => setView({ ...M.vb, x: M.vb.x + step }), ArrowUp: () => setView({ ...M.vb, y: M.vb.y - step }), ArrowDown: () => setView({ ...M.vb, y: M.vb.y + step }), "+": () => zoom(0.7), "=": () => zoom(0.7), "-": () => zoom(1.4), "0": () => setView(FULL) }[k];
    if (act) { e.preventDefault(); act(); }
  });
  document.querySelectorAll(".map-tools button[data-zoom]").forEach(b => b.onclick = () => ({ in: () => zoom(0.7), out: () => zoom(1.4), reset: () => setView(FULL), fit: fitTop })[b.dataset.zoom]());
}
function setView(vb) {
  vb.w = Math.max(25, Math.min(FULL.w * 1.2, vb.w)); vb.h = vb.w * FULL.h / FULL.w;
  M.vb = vb;
  M.svg.setAttribute("viewBox", `${vb.x.toFixed(1)} ${vb.y.toFixed(1)} ${vb.w.toFixed(1)} ${vb.h.toFixed(1)}`);
  const s = Math.pow(vb.w / FULL.w, 0.75);   // markers shrink slower than the map grows
  M.svg.querySelectorAll(".mk").forEach(g => g.setAttribute("transform", `translate(${g.dataset.x} ${g.dataset.y}) scale(${(s * (+g.dataset.k || 1)).toFixed(3)})`));
}
function zoom(f) { const v = M.vb, cx = v.x + v.w / 2, cy = v.y + v.h / 2, w = v.w * f, h = v.h * f; setView({ x: cx - w / 2, y: cy - h / 2, w, h }); }
function fitPoints(pts, pad = 26) {
  if (!pts.length) return setView(FULL);
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const w = Math.max(Math.max(...xs) - Math.min(...xs) + 2 * pad, (Math.max(...ys) - Math.min(...ys) + 2 * pad) * FULL.w / FULL.h, 70);
  setView({ x: cx - w / 2, y: cy - (w * FULL.h / FULL.w) / 2, w, h: 0 });
}
function fitTop() {
  const r = S.result; if (!r) return;
  fitPoints([...r.top3.map(t => M.zones.get(t.zone_id).pt), px(r.work.lon, r.work.lat)]);
}

function paintMap() {
  const r = S.result; if (!r) return;
  const deserts = S.view === "deserts";
  for (const [id, z] of M.zones) {
    const st = r.zone_status[id] || { status: "no_rent_data" };
    let fill;
    if (deserts) fill = st.status === "desert" ? "url(#p-desert)" : st.status === "qualifies" ? "#9ad1c2" : "#efe9db";
    else fill = st.status === "qualifies" ? viridis(scoreT(st.score)) : (st.status === "no_rent_data" || st.status === "little_housing") ? "url(#p-dot)" : (st.status === "desert" || st.status === "no_station") ? "url(#p-x)" : "#f3ede0";
    z.path.setAttribute("fill", fill);
  }
  M.gTop.innerHTML = ""; M.gBadge.innerHTML = ""; M.gWork.innerHTML = ""; M.gLift.innerHTML = "";
  // optional 3D: qualifying zones become prisms whose height is the score (drawn north → south so nearer ones overlap)
  const lift = M.lift && !deserts, H = id => lift && r.zone_status[id]?.status === "qualifies" ? 2 + 16 * Math.max(0, Math.min(1, scoreT(r.zone_status[id].score))) : 0;
  M.gStn.style.display = lift ? "none" : "";
  if (lift) for (const t of [...r.ranked].sort((a, b) => M.zones.get(a.zone_id).pt[1] - M.zones.get(b.zone_id).pt[1])) {
    const z = M.zones.get(t.zone_id), d = z.path.getAttribute("d"), h = H(t.zone_id);
    for (let i = 0.6; i < h; i += 0.6) M.gLift.append(sv("path", { d, class: "wall", fill: "#14110f", transform: `translate(0 ${(-i).toFixed(1)})` }));
    const roof = sv("path", { d, class: "z", fill: viridis(scoreT(t.score)), transform: `translate(0 ${-h.toFixed(1)})`, "fill-rule": "evenodd" });
    roof.dataset.id = t.zone_id; M.gLift.append(roof);
  }
  if (!deserts) for (const t of r.top3) {
    const z = M.zones.get(t.zone_id), d = z.path.getAttribute("d"), tf = `translate(0 ${-H(t.zone_id).toFixed(1)})`;
    M.gTop.append(sv("path", { d, class: "top-o", transform: tf }), sv("path", { d, class: "top-i", transform: tf }));
    const g = sv("g", { class: "mk badge", "data-x": z.pt[0], "data-y": z.pt[1] - H(t.zone_id), "data-k": 1 });
    g.append(sv("circle", { r: 9 })); const tx = sv("text"); tx.textContent = t.rank; g.append(tx);
    M.gBadge.append(g);
  }
  const [wx, wy] = px(r.work.lon, r.work.lat);
  const star = sv("g", { class: "mk", "data-x": wx, "data-y": wy });
  let d = ""; for (let i = 0; i < 10; i++) { const a = Math.PI / 5 * i - Math.PI / 2, rr = i % 2 ? 4.2 : 10; d += (i ? "L" : "M") + (rr * Math.cos(a)).toFixed(2) + " " + (rr * Math.sin(a)).toFixed(2); }
  star.append(sv("path", { d: d + "Z", class: "work" })); M.gWork.append(star);
  setView(M.vb);
  renderLegend();
}
function renderLegend() {
  const sw = (fill, extra = "") => `<svg width="26" height="18" aria-hidden="true"><rect width="26" height="18" fill="${fill}"/>${extra}</svg>`;
  const mk = inner => `<svg width="26" height="18" aria-hidden="true" viewBox="-13 -9 26 18">${inner}</svg>`;
  const ramp = `<svg class="ramp" aria-hidden="true" viewBox="0 0 96 18" preserveAspectRatio="none">${VIR.map((c, i) => `<rect x="${i * 96 / 9}" width="${96 / 9 + .5}" height="18" fill="${c}"/>`).join("")}</svg>`;
  const pat = id => `<svg width="26" height="18" aria-hidden="true"><rect width="26" height="18" fill="url(#${id})"/></svg>`;
  const items = S.view === "deserts" ? [
    [pat("p-desert"), "Desert: affordable, no accessible station, slow rides (striped red)"],
    [sw("#9ad1c2"), "Qualifies"], [sw("#efe9db"), "Everything else"],
  ] : [
    [ramp, "Score: 30 (purple) → 90 (yellow). Exact numbers are in the table"],
    [pat("p-x"), "Affordable, but no accessible station (cross-hatched)"],
    [sw("#f3ede0"), "Over budget (plain)"], [pat("p-dot"), "No rent data or very little housing (dotted)"],
  ];
  items.push(
    [mk('<circle r="5" fill="#0b6e4f"/>'), "Accessible station, elevators up 95%+ (circle)"],
    [mk('<path d="M0 -6L6 5L-6 5Z" fill="#e69f00"/>'), "Elevators up 85–95% (triangle)"],
    [mk('<rect x="-5" y="-5" width="10" height="10" fill="#a3211a"/>'), "Elevators up under 85% (square)"],
    [mk('<path d="M0 -6L6 0L0 6L-6 0Z" fill="#5f5a52"/>'), "No elevator data (diamond)"],
    [mk('<path d="M0 -8L2.2 -2.6L8 -2.4L3.4 1.2L5 7L0 3.6L-5 7L-3.4 1.2L-8 -2.4L-2.2 -2.6Z" fill="#d81b60" stroke="#14110f"/>'), "Your workplace (star)"],
    [mk('<circle r="7" fill="#ffd400" stroke="#14110f" stroke-width="2"/>'), "Top three, numbered and outlined"]);
  $("#legend").innerHTML = items.map(([s, t]) => `<li>${s}<span>${esc(t)}</span></li>`).join("");
}

/* ---------- street view ---------- */
function streetHtml(lat, lon, what) {
  if (lat == null) return "";
  const url = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat.toFixed(6)},${lon.toFixed(6)}`;
  const embed = S.boot.gmaps_key ? ` <button type="button" class="chip" data-street="${lat.toFixed(6)},${lon.toFixed(6)}" aria-expanded="false">Show the street here</button>` : "";
  return `<p><a class="street" href="${url}" target="_blank" rel="noopener">See the street at ${esc(what)}<span class="vh"> in Google Street View</span> ↗ <small>(Street View, new tab)</small></a>${embed}</p>`;
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-street]"); if (!b) return;
  const open = b.getAttribute("aria-expanded") === "true", host = b.closest("p");
  host.parentNode.querySelector(".street-frame")?.remove(); b.setAttribute("aria-expanded", !open);
  if (!open) host.insertAdjacentHTML("afterend", `<iframe class="street-frame" title="Google Street View at this location" loading="lazy" allowfullscreen referrerpolicy="no-referrer" src="https://www.google.com/maps/embed/v1/streetview?key=${encodeURIComponent(S.boot.gmaps_key)}&location=${b.dataset.street}"></iframe>`);
});

/* ---------- zone inspector + selection ---------- */
function zoneText(id) {
  const r = S.result, z = M.zones.get(id); if (!z) return "";
  const name = `<h3>${esc(z.props.zone_name)} <span class="sc-boro">· ${esc(z.props.borough)}</span></h3>`;
  if (!r) return name;
  const st = r.zone_status[id] || { status: "no_rent_data" };
  if (st.status === "qualifies") {
    const t = r.ranked[st.rank - 1];
    return `${name}<p><b>#${t.rank} of ${r.ranked.length}</b> · score <b>${t.score.toFixed(0)}</b> · typical rent <b>${money(t.rent)}</b></p><p>${esc(t.commute.text)}.</p><p>Biggest risk: ${esc(t.risk)}.</p>${t.home_station ? streetHtml(t.home_station.lat, t.home_station.lon, t.home_station.name + " station") : ""}`;
  }
  const why = { over_budget: `Typical rent ${money(st.rent || 0)} is over your budget of ${money(r.params.budget)}.`, no_rent_data: "No rent data for this zone, so it is not ranked.", little_housing: "Fewer than 1,000 apartments here (park, airport or industrial area), so it is not ranked.", desert: `Accessibility desert: typical rent ${money(st.rent || 0)} fits the budget, but there is no accessible subway station in reach and accessible ride-hail is slow or too rare to measure.`, no_station: `Typical rent ${money(st.rent || 0)} fits the budget, but there is no accessible subway station in reach.` }[st.status];
  return `${name}<p>${esc(why)}</p>${streetHtml(z.props.label_lat, z.props.label_lon, "the middle of this zone")}`;
}
function inspect(id) { $("#inspector").innerHTML = zoneText(id); }
function select(id, { fromMap = false, zoomTo = false } = {}) {
  if (S.sel) M.zones.get(S.sel)?.path.classList.remove("sel");
  document.querySelectorAll(".hl").forEach(e => e.classList.remove("hl"));
  S.sel = id; const z = M.zones.get(id); if (!z) return;
  z.path.classList.add("sel"); inspect(id);
  document.querySelectorAll(`[data-zone="${id}"]`).forEach(e => e.classList.add("hl"));
  if (zoomTo) fitPoints([z.pt], 60);
  if (fromMap) { const row = $(`#ranked tr[data-zone="${id}"]`); if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}

/* ---------- renderers ---------- */
function renderDots() {
  const c = S.boot.city, n = c.stations, full = c.ada_full, part = c.ada_partial;
  let h = ""; for (let i = 0; i < n; i++) h += `<i class="${i < full ? "on" : i < full + part ? "half" : ""}"></i>`;
  $("#dots").innerHTML = h;
  $("#dots-cap").innerHTML = `Every subway station as a dot. Filled: the <b>${full} of ${n}</b> you can use in a wheelchair${part ? ` (half-filled: ${part} more in one direction only)` : ""}.`;
}
function renderLanding() {
  const L = S.boot.landing, c = S.boot.city, cov = L.coverage, w = L.waits, far = 1 - cov.units_near_ada / cov.units;
  // cover map: black = an accessible station in reach, hatched = none
  const svg = sv("svg", { viewBox: `0 0 ${FULL.w.toFixed(0)} ${FULL.h.toFixed(0)}`, "aria-hidden": "true", focusable: "false" });
  svg.innerHTML = `<defs><pattern id="p-cx" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="#fffdf8"/><rect width="1.6" height="5" fill="#a3211a"/></pattern></defs>`;
  for (const f of S.boot.zones.features) {
    const n = L.has_station[f.properties.zone_id];
    svg.append(sv("path", { d: geomPath(f.geometry), "fill-rule": "evenodd", fill: n > 0 ? "#14110f" : n === 0 ? "url(#p-cx)" : "#e9e2d2", stroke: "#fffdf8", "stroke-width": ".45" }));
  }
  for (const s of S.boot.stations) { const [x, y] = px(s.lon, s.lat); svg.append(sv("circle", { cx: x, cy: y, r: 1.7, fill: "#ffd400" })); }
  $("#cover-map").append(svg);
  $("#cover-cap").innerHTML = `<span class="key" style="background:#14110f"></span><b>Black:</b> an accessible subway station is within reach (yellow dots). <span class="key" style="background:repeating-linear-gradient(45deg,#a3211a 0 2px,#fffdf8 2px 5px)"></span><b>Striped:</b> none — <b>${cov.zones_no_station} of ${cov.zones}</b> residential neighbourhoods. Citywide, <b>${Math.round(far * 100)}%</b> of apartments are more than 800 m from an accessible station.`;
  $("#lede").innerHTML = `Listing sites show price and photos. They do not show whether you can get out of the neighbourhood. For the <b>${L.ambulatory.n.toLocaleString("en-US")} New Yorkers</b> who have serious difficulty walking or climbing stairs, that is the whole question.`;
  const extra = Math.round((w.wav_wait_median_min / w.other_wait_median_min - 1) * 100);
  $("#stats").innerHTML = [
    [L.ambulatory.n.toLocaleString("en-US"), "New Yorkers have serious difficulty walking or climbing stairs", L.ambulatory.source],
    [`1 in ${Math.round(c.stations / c.ada_full)}`, `subway stations is fully accessible — ${c.ada_full} of ${c.stations}`, "MTA station list"],
    [`${Math.round(c.worst_uptime * 100)}%`, `of the time: how often the elevators at ${c.worst_station} worked, April–October 2026`, "MTA elevator availability"],
    [`+${extra}%`, `longer wait for a wheelchair-accessible Uber or Lyft: ${w.wav_wait_median_min.toFixed(1)} vs ${w.other_wait_median_min.toFixed(1)} minutes`, "21 million TLC trips, Sept 2026"],
  ].map(([n, t, src]) => `<li><b>${esc(n)}</b><span>${esc(t)}</span><small>${esc(src)}</small></li>`).join("");
  const p = L.pair;
  if (p) $("#pair").innerHTML = `<h2 id="h-pair" class="eyebrow"><span class="stop" aria-hidden="true">!</span> Two neighbourhoods in ${esc(p.borough === "Bronx" ? "the Bronx" : p.borough)}. Almost the same rent.</h2>
    <div class="pair-grid">
      <article class="listing good"><h3>${esc(p.a_name)}</h3><p class="price">${money(p.a_rent)} <small>typical rent / month</small></p><ul>
        <li><span class="ic" aria-hidden="true">+</span><span>${p.a_n} accessible stations in reach: ${esc(p.a_stations)}</span></li>
        <li><span class="ic" aria-hidden="true">+</span><span>Their elevators worked ${pct(p.a_uptime, 1)} of the time</span></li>
        <li><span class="ic" aria-hidden="true">+</span><span>${pct(p.a_near)} of apartments are within 800 m of one</span></li></ul>
        <p class="verdict">On the map</p></article>
      <p class="vs" aria-hidden="true">versus</p>
      <article class="listing bad"><h3>${esc(p.b_name)}</h3><p class="price">${money(p.b_rent)} <small>typical rent / month</small></p><ul>
        <li><span class="ic" aria-hidden="true">–</span><span>No accessible subway station in reach</span></li>
        <li><span class="ic" aria-hidden="true">–</span><span>Accessible Uber/Lyft: ${p.b_wav.toFixed(1)} min typical wait, 1 in 10 over ${p.b_wav_p90.toFixed(0)} min</span></li>
        <li><span class="ic" aria-hidden="true">–</span><span>${p.b_units.toLocaleString("en-US")} apartments, ${pct(p.b_near)} near an accessible station</span></li></ul>
        <p class="verdict">Off the map</p></article>
    </div>
    <p class="pair-note">On a listings site these two look the same. Real neighbourhoods, real numbers from the data loaded tonight — typical market rents, not accessible-unit rents.</p>`;
}
function stationHtml(s) {
  if (!s) return NODATA;
  return `${bullets(s.routes)} ${esc(s.name)}<span class="vh"> (lines ${esc(routeWords(s.routes))})</span>`;
}
const commuteShort = t => ({ one_seat: ['yes', 'One-seat ride'], transfer: ['', '1 transfer'], no_route: ['no', 'No route found'], no_work_station: ['no', 'No station at work'], no_home_station: ['no', 'No station at home'] }[t.commute.kind]);
const PARTS = [["housing", "Housing"], ["transit", "Subway"], ["street", "Ramps"], ["plan_b", "Plan B rides"], ["rent", "Rent room"]];
function barHtml(t, w) {
  return `<div class="bar" aria-hidden="true">${PARTS.map(([k], i) => `<i class="p${i + 1}" style="flex:${w[k].max}"><u style="width:${100 * t.components[k] / w[k].max}%"></u></i>`).join("")}</div>
  <p class="parts">${PARTS.map(([k, l]) => `<span>${l} <b>${t.components[k].toFixed(0)}</b>/${w[k].max}</span>`).join("")}</p>`;
}
function renderTop3() {
  const r = S.result, c = r.counts, ol = $("#top3");
  $("#filter-line").innerHTML = `Of ${c.zones} zones, <b>${c.affordable}</b> have a typical rent within ${money(r.params.budget)}. Only <b>${c.qualifying}</b> of those also have an accessible subway station in reach.` + (r.notes.length ? ` <b>${esc(r.notes[0])}</b>` : "");
  $("#top3-title").textContent = r.top3.length >= 3 ? "Your top three" : r.top3.length ? "Your best options" : "No match";
  if (!r.top3.length) { ol.innerHTML = `<li class="empty">No neighbourhood has a typical rent within ${money(r.params.budget)} and an accessible station in reach. Move the budget dial up to see where the map opens.</li>`; return; }
  ol.innerHTML = r.top3.map(t => {
    const hs = t.home_station, cs = commuteShort(t);
    return `<li class="stopcard" data-rank="${t.rank}" data-zone="${t.zone_id}">
      <div class="sc-head"><div><h3><span class="vh">Number ${t.rank}: </span>${esc(t.zone_name)}</h3><p class="sc-boro">${esc(t.borough)} · ${t.dist_work_km} km from work in a straight line</p></div>
        <p class="sc-score"><b>${t.score.toFixed(0)}</b><span>out of 100</span></p></div>
      ${barHtml(t, r.weights)}
      <dl class="facts">
        <div><dt>Typical rent</dt><dd><span class="big">${money(t.rent)}</span><small>${money(t.rent_headroom)} under budget</small></dd></div>
        <div><dt>Nearest accessible station</dt><dd>${stationHtml(hs)}<small>${hs && hs.dist_m != null ? hs.dist_m + " m from the zone’s centre of housing" : ""}</small></dd></div>
        <div><dt>Its elevators worked</dt><dd><span class="big">${hs ? pct(hs.uptime, 1) : NODATA}</span><small>of the time, Apr–Oct 2026${hs && hs.uptime_worst != null && hs.n_elevators > 1 ? `; worst of ${hs.n_elevators} elevators: ${pct(hs.uptime_worst)}` : ""}</small></dd></div>
        <div><dt>Ride to work</dt><dd><span class="pill ${cs[0]}">${cs[1]}</span><small>${t.commute.kind === "one_seat" ? `${bullets(t.commute.routes)} to ${esc(t.commute.work_station.name)}` : esc(t.commute.text)}</small></dd></div>
        <div><dt>Plan B: ride to work</dt><dd>${t.plan_b_ride ? `<span class="big">${money(t.plan_b_ride.fare)}</span><small>and about ${Math.round(t.plan_b_ride.minutes)} min by Uber/Lyft (typical of ${t.plan_b_ride.trips.toLocaleString("en-US")} trips)${t.plan_b_ride.monthly_cost != null ? `. If the elevator fails as often as it did: about <b>${money(t.plan_b_ride.monthly_cost)} a month</b> in rides` : ""}</small>` : '<span class="nodata">too few trips on this route</span>'}</dd></div>
        <div><dt>Accessible Uber/Lyft wait</dt><dd><span class="big">${mins(t.wav_wait_median_min)}</span><small>${t.wav_wait_p90_min != null ? `typical; 1 in 10 waits over ${t.wav_wait_p90_min.toFixed(0)} min` : ""}</small></dd></div>
      </dl>
      <ul class="pm">
        ${t.pluses.map(p => `<li class="plus"><span class="ic" aria-hidden="true">+</span><span><span class="vh">Plus: </span>${esc(p)}</span></li>`).join("")}
        <li class="risk"><span class="ic" aria-hidden="true"><span>!</span></span><span><span class="lbl">Biggest risk</span>${esc(t.risk)}</span></li>
      </ul>${hs ? streetHtml(hs.lat, hs.lon, hs.name + " station") : ""}</li>`;
  }).join("");
  ol.querySelectorAll(".stopcard").forEach(li => { li.addEventListener("pointerenter", () => select(+li.dataset.zone)); });
}
const COLS = [
  { key: "rank", label: "Rank", num: true, get: t => t.rank, html: t => t.rank },
  { key: "zone_name", label: "Neighbourhood", get: t => t.zone_name, head: true, html: t => `<button type="button" class="zone-btn" data-zone-btn="${t.zone_id}">${esc(t.zone_name)}<span class="vh">, show on map</span></button><span class="sub">${esc(t.borough)}</span>` },
  { key: "score", label: "Score", num: true, get: t => t.score, html: t => `<b>${t.score.toFixed(0)}</b>` },
  { key: "rent", label: "Typical rent", num: true, get: t => t.rent, html: t => money(t.rent) },
  { key: "station", label: "Accessible station", get: t => t.home_station?.name || "", html: t => stationHtml(t.home_station) },
  { key: "uptime", label: "Elevators up", num: true, get: t => t.home_station?.uptime ?? -1, html: t => t.home_station ? pct(t.home_station.uptime, 1) : NODATA },
  { key: "commute", label: "Ride to work", get: t => ({ one_seat: 0, transfer: 1 }[t.commute.kind] ?? 2), html: t => { const c = commuteShort(t); return `<span class="pill ${c[0]}">${c[1]}</span>`; } },
  { key: "wav", label: "Accessible ride wait", num: true, get: t => t.wav_wait_median_min ?? 99, html: t => mins(t.wav_wait_median_min) },
  { key: "fare", label: "Plan B fare", num: true, get: t => t.plan_b_ride?.fare ?? 999, html: t => t.plan_b_ride ? `${money(t.plan_b_ride.fare)}<span class="sub">${Math.round(t.plan_b_ride.minutes)} min</span>` : '<span class="nodata">too few trips</span>' },
  { key: "dist", label: "From work", num: true, get: t => t.dist_work_km, html: t => t.dist_work_km + " km" },
  { key: "risk", label: "Biggest risk", get: t => t.risk, html: t => esc(t.risk) },
];
function renderTable() {
  const r = S.result, tb = $("#ranked"), { key, dir } = S.sort, col = COLS.find(c => c.key === key);
  const rows = [...r.ranked].sort((a, b) => { const x = col.get(a), y = col.get(b); return (x < y ? -1 : x > y ? 1 : a.rank - b.rank) * dir; });
  tb.innerHTML = `<caption>${rows.length} neighbourhoods within ${money(r.params.budget)} that have an accessible subway station, sorted by ${esc(col.label.toLowerCase())}.</caption>
    <thead><tr>${COLS.map(c => `<th scope="col" ${c.key === key ? `aria-sort="${dir > 0 ? "ascending" : "descending"}"` : ""}><button type="button" data-sort="${c.key}">${esc(c.label)}</button></th>`).join("")}</tr></thead>
    <tbody>${rows.map(t => `<tr data-zone="${t.zone_id}">${COLS.map(c => c.head ? `<th scope="row">${c.html(t)}</th>` : `<td class="${c.num ? "num" : ""}">${c.html(t)}</td>`).join("")}</tr>`).join("")}</tbody>`;
  if (S.sel) tb.querySelector(`tr[data-zone="${S.sel}"]`)?.classList.add("hl");
}
$("#ranked").addEventListener("click", e => {
  const sb = e.target.closest("[data-sort]"), zb = e.target.closest("[data-zone-btn]");
  if (sb) { const k = sb.dataset.sort; S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : (["score", "uptime"].includes(k) ? -1 : 1) }; renderTable(); $(`#ranked [data-sort="${k}"]`).focus(); say(`Sorted by ${sb.textContent}, ${S.sort.dir > 0 ? "ascending" : "descending"}`); }
  if (zb) { select(+zb.dataset.zoneBtn, { zoomTo: true }); }
});
$("#ranked").addEventListener("pointerover", e => { const tr = e.target.closest("tr[data-zone]"); if (tr && +tr.dataset.zone !== S.sel) select(+tr.dataset.zone); });
$("#ranked").addEventListener("focusin", e => { const tr = e.target.closest("tr[data-zone]"); if (tr && +tr.dataset.zone !== S.sel) select(+tr.dataset.zone); });

function renderDeserts() {
  const r = S.result, d = r.deserts;
  $("#deserts-line").innerHTML = d.length ? `<b>${d.length} zones</b> with about <b>${r.counts.desert_units.toLocaleString("en-US")} apartments</b> look affordable at ${money(r.params.budget)} — but have no accessible subway station in reach, and accessible Uber/Lyft is slower than the city’s typical ${r.wav_city_median_min.toFixed(1)} minutes or too rare to measure. For a wheelchair user, this part of the affordable map is not really there.` : `No accessibility deserts at ${money(r.params.budget)}.`;
  $("#deserts-table").innerHTML = `<caption>Affordable zones without an accessible station, slowest accessible rides first.</caption><thead><tr>${["Neighbourhood", "Typical rent", "Accessible ride wait", "1 in 10 wait over", "Apartments near an accessible station", "Apartments"].map(h => `<th scope="col"><span>${h}</span></th>`).join("")}</tr></thead>
  <tbody>${d.map(z => `<tr data-zone="${z.zone_id}"><th scope="row"><button type="button" class="zone-btn" data-zone-btn="${z.zone_id}">${esc(z.zone_name)}</button><span class="sub">${esc(z.borough)}</span></th><td class="num">${money(z.rent)}</td><td class="num">${mins(z.wav_wait_median_min)}</td><td class="num">${z.wav_wait_p90_min != null ? z.wav_wait_p90_min.toFixed(0) + " min" : NODATA}</td><td class="num">${pct(z.units_near_ada_share)}</td><td class="num">${z.mf_units.toLocaleString("en-US")}</td></tr>`).join("")}</tbody>`;
}
$("#deserts-table").addEventListener("click", e => { const zb = e.target.closest("[data-zone-btn]"); if (zb) select(+zb.dataset.zoneBtn, { zoomTo: true }); });

function renderUnderstood() {
  const r = S.result, w = r.work;
  $("#understood").innerHTML = `<span class="tag"><small>Budget</small>${money(r.params.budget)} / month</span>
    <span class="tag"><small>Work</small>${esc(w.label || "")} → ${esc(w.zone ? w.zone.zone_name : "")}</span>
    <span class="tag"><small>Accessible stations near work</small>${w.stations.length ? w.stations.length + " within 800 m" : "none within 800 m"}</span>`;
}
function renderHow() {
  const r = S.result;
  if (r) $("#weights").innerHTML = `<ul class="wts">${Object.values(r.weights).map(w => `<li><span class="w">${w.max}</span><span><b>${esc(w.label)}</b><br>${esc(w.how)}</span></li>`).join("")}</ul><p style="margin-top:12px">Hard filters first: typical rent within budget, at least 1,000 apartments in the zone, and an accessible station in reach of home. Missing data scores 0 for that part and is labelled; it is never filled in.</p>`;
  const blocks = S.sqls.map(q => `<div class="sqlblock"><h3>${esc(q.purpose)} <span class="by">— ${q.by === "model" ? "written by the model" : q.by === "rules" ? "fixed check" : "scoring method"}</span></h3><pre><code>${esc(q.sql)}</code></pre>${q.error ? `<p><b>Error:</b> ${esc(q.error)}</p>` : q.columns ? `<table class="mini"><thead><tr>${q.columns.map(c => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead><tbody>${q.rows.map(row => `<tr>${row.map(v => `<td>${v == null ? "no data" : esc(typeof v === "number" && !Number.isInteger(v) ? +v.toFixed(4) : v)}</td>`).join("")}</tr>`).join("")}</tbody></table>${q.row_count > q.rows.length ? `<p class="by">showing ${q.rows.length} of ${q.row_count} rows</p>` : ""}` : ""}</div>`);
  if (r) blocks.push(`<div class="sqlblock"><h3>How the ranking is computed <span class="by">— scoring method</span></h3><pre><code>${esc(r.method_sql)}</code></pre></div>`);
  $("#sqls").innerHTML = blocks.join(""); $("#sql-count").textContent = S.sqls.length ? `(${S.sqls.length} + method)` : "";
  const all = [...S.runChecks.map(c => ({ name: c.name, value: "", detail: c.detail, ok: c.ok })), ...S.boot.checks];
  $("#checks").innerHTML = `<ul class="checks">${all.map(c => `<li><span class="${c.ok ? "ok" : "bad"}">${c.ok ? "OK" : "!!"}</span><div><b>${esc(c.name)}${c.value ? ": " + esc(c.value) : ""}</b> <span>${esc(c.detail)}</span></div></li>`).join("")}</ul>`;
  $("#check-count").textContent = `(${all.filter(c => c.ok).length} of ${all.length} OK)`;
}
function renderResult(data, { fromDial = false } = {}) {
  S.result = data;
  $("#results").hidden = false;
  renderUnderstood(); renderTop3(); renderTable(); renderDeserts(); paintMap(); renderHow();
  if (!fromDial) { $("#budget").value = Math.max(1500, Math.min(8000, data.params.budget)); $("#dial-out").textContent = money(data.params.budget); S.answerBudget = data.params.budget; $("#dial-note").hidden = true; fitTop(); }
  else { const diff = data.params.budget !== S.answerBudget; $("#dial-note").hidden = !diff; $("#dial-note").textContent = diff ? `The written answer above was for ${money(S.answerBudget)}. The cards, table and map now show ${money(data.params.budget)}.` : ""; }
}

/* ---------- ask flow ---------- */
const say = t => { $("#status").textContent = ""; setTimeout(() => { $("#status").textContent = t; }, 30); };
function addStep(ev) {
  const tr = $("#trace"); tr.querySelectorAll("li.active").forEach(li => li.remove());
  tr.appendChild(el("li", { class: ev.status || "done" }, `<b>${esc(ev.label)}</b>${ev.detail ? ` <span>— ${esc(ev.detail)}</span>` : ""}`));
}
async function ask(question, fresh = false) {
  if (S.busy) return; S.busy = true;
  const go = $("#go"); go.disabled = true; go.querySelector("span").textContent = "Working…";
  S.sqls = []; S.runChecks = []; let buf = "", gotResult = false;
  history.replaceState(null, "", "?q=" + encodeURIComponent(question));   // shareable link
  $("#trace").innerHTML = ""; $("#trace-wrap").hidden = false; $("#h-trace").lastChild.textContent = " Working on it";
  $("#answer-text").innerHTML = ""; $("#answer-by").textContent = ""; $("#png").innerHTML = ""; $("#mode-note").hidden = true;
  say("Working on your question.");
  const at = $("#answer-text");
  const handle = ev => {
    if (ev.type === "mode") {
      if (ev.mode === "offline") { const n = $("#mode-note"); n.hidden = false; n.textContent = ev.reason; }
      S.mode = ev;
    } else if (ev.type === "step") addStep(ev);
    else if (ev.type === "sql") { S.sqls.push(ev); addStep({ label: ev.by === "model" ? "Checked its own work with SQL" : "Ran a fixed check", detail: ev.purpose, status: ev.error ? "warn" : "done" }); if (S.result) renderHow(); }
    else if (ev.type === "check") { S.runChecks.push(ev); addStep({ label: ev.name, detail: ev.detail, status: ev.ok ? "done" : "warn" }); }
    else if (ev.type === "result") { gotResult = true; renderResult(ev.data); }
    else if (ev.type === "map") { $("#png").innerHTML = `<img src="/api/map/${esc(ev.id)}.png" alt="Static map: New York City zones coloured by score, the top three outlined and numbered, the workplace as a star. The same information is in the table above."><p>${ev.seen_by_model ? "This exact image was sent back to the model, which compared it with its own top three." : "No model was connected, so this image was rendered but not inspected."}</p>`; }
    else if (ev.type === "answer_delta") { $("#results").hidden = false; buf += ev.text; at.classList.add("streaming"); at.innerHTML = md(buf); }
    else if (ev.type === "answer_reset") { buf = ""; at.innerHTML = ""; at.classList.remove("streaming"); }
    else if (ev.type === "answer") { $("#results").hidden = false; at.classList.remove("streaming"); at.innerHTML = md(ev.text); $("#answer-by").textContent = ev.model ? `Written by ${ev.model} after running the queries below and looking at the rendered map.${S.mode && S.mode.cached_at ? ` (Replayed from a run at ${S.mode.cached_at}.)` : ""}` : "Written by fixed rules — no AI model was involved in this answer."; }
    else if (ev.type === "error") { addStep({ label: "Something went wrong", detail: ev.message, status: "warn" }); }
    else if (ev.type === "done") { $("#h-trace").lastChild.textContent = ` What happened (${ev.seconds} s${ev.cached ? ", replayed" : ""})`; }
  };
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, fresh }) });
    if (!res.ok) throw new Error("server said " + res.status);
    const reader = res.body.getReader(), dec = new TextDecoder(); let tail = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      tail += dec.decode(value, { stream: true });
      const lines = tail.split("\n"); tail = lines.pop();
      for (const l of lines) if (l.trim()) handle(JSON.parse(l));
    }
  } catch (e) { addStep({ label: "Could not reach the app server", detail: String(e.message || e), status: "warn" }); }
  S.busy = false; go.disabled = false; go.querySelector("span").textContent = "Find my neighbourhoods";
  $("#trace").querySelectorAll("li.active").forEach(li => li.remove());
  const r = S.result;
  if (gotResult && r) { say(r.top3.length ? `Done. Top three: ${r.top3.map(t => `${t.zone_name}, score ${t.score.toFixed(0)}`).join("; ")}.` : "Done. No neighbourhood matched."); $("#results").focus({ preventScroll: true }); $("#results").scrollIntoView({ behavior: "smooth", block: "start" }); }
  else say("Done. " + at.textContent.slice(0, 200));
}
$("#ask-form").addEventListener("submit", e => { e.preventDefault(); const q = $("#q").value.trim(); if (q) ask(q, e.submitter && e.shiftKey); });
$("#q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
document.querySelectorAll(".chip").forEach(c => c.onclick = () => { $("#q").value = c.dataset.q; ask(c.dataset.q); });

$("#cta").addEventListener("click", e => { e.preventDefault(); $("#tool").scrollIntoView({ behavior: "smooth" }); $("#q").focus({ preventScroll: true }); });
$("#cta-demo").addEventListener("click", () => { const q = document.querySelector(".chip").dataset.q; $("#q").value = q; $("#tool").scrollIntoView({ behavior: "smooth" }); ask(q); });

/* ---------- dial + deserts ---------- */
let dialT = null, dialSeq = 0;
$("#budget").addEventListener("input", e => {
  const b = +e.target.value; $("#dial-out").textContent = money(b);
  clearTimeout(dialT); dialT = setTimeout(async () => {
    const r = S.result; if (!r) return; const seq = ++dialSeq;
    const res = await fetch("/api/score", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ budget: b, work_lat: r.params.work_lat, work_lon: r.params.work_lon, work_label: r.params.work_label }) });
    if (!res.ok || seq !== dialSeq) return;
    const d = await res.json(); renderResult(d, { fromDial: true });
    say(`${money(b)}: ${d.counts.qualifying} neighbourhoods qualify.` + (d.top3[0] ? ` First is ${d.top3[0].zone_name}.` : ""));
  }, 140);
});
$("#deserts").addEventListener("click", e => {
  const b = e.currentTarget, on = b.getAttribute("aria-checked") !== "true";
  b.setAttribute("aria-checked", on); S.view = on ? "deserts" : "score";
  $("#deserts-sec").hidden = !on; $("#top3-sec").hidden = on;
  paintMap(); if (on) setView(FULL); else fitTop();
  say(on ? `Showing ${S.result.deserts.length} accessibility deserts.` : "Showing your top three.");
});

/* ---------- boot ---------- */
fetch("/api/bootstrap").then(r => r.json()).then(b => {
  S.boot = b; renderLanding(); renderDots(); buildMap(); renderLegend(); renderHow();
  const q0 = new URLSearchParams(location.search).get("q");
  const sp = new URLSearchParams(location.search);   // deep-link extras: &view=deserts &lift=1 &theme=ink
  if (sp.get("theme")) { prefs.theme = sp.get("theme"); applyPrefs(); }
  if (q0) { $("#q").value = q0; ask(q0).then(() => { if (!S.result) return; if (sp.get("lift")) $("#lift").click(); if (sp.get("view") === "deserts") $("#deserts").click(); }); }
  if (!b.live) { const n = $("#mode-note"); n.hidden = false; n.textContent = "No Claude API key found, so answers use fixed rules over the same data. Set ANTHROPIC_API_KEY (or add it to .env) and restart to let the model plan, check and read the map."; }
}).catch(err => { console.error(err); $("#dots-cap").textContent = "Could not load the data. Is the server running, and has app/build_data.py been run?"; });
})();
