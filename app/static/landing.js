// Landing page numbers (was an inline script; moved out so the CSP can forbid inline script).
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = n => "$" + Math.round(n).toLocaleString("en-US");
const prefs = JSON.parse(localStorage.getItem("wnyc-prefs") || "{}");
if (prefs.theme === "ink") document.documentElement.dataset.theme = "ink";
fetch("/api/landing").then(r => r.json()).then(L => {
  const c = L.city, w = L.waits, p = L.pair;
  document.getElementById("lede").innerHTML = `An apartment listing shows the price. It does not show whether you can get out of the neighbourhood. For the <b>${L.ambulatory.n.toLocaleString("en-US")} New Yorkers</b> who have serious difficulty walking or climbing stairs, that is the whole question. This map answers it.`;
  document.getElementById("stats").innerHTML = [
    [`1 in ${Math.round(c.stations / c.ada_full)}`, `subway stations is fully accessible (${c.ada_full} of ${c.stations})`],
    [`${Math.round(c.worst_uptime * 100)}%`, `of the time the elevators at ${c.worst_station} worked, Apr–Oct 2026`],
    [`+${Math.round((w.wav_wait_median_min / w.other_wait_median_min - 1) * 100)}%`, `longer wait for a wheelchair-accessible Uber or Lyft`],
  ].map(([n, t]) => `<li><b>${esc(n)}</b><span>${esc(t)}</span></li>`).join("");
  if (p) document.getElementById("pair").innerHTML = `<h2>Two neighbourhoods in ${p.borough === "Bronx" ? "the Bronx" : esc(p.borough)}. On a listings site they look the same.</h2>
    <article class="listing"><h3>${esc(p.a_name)} <span>${money(p.a_rent)}</span></h3><p>${p.a_n} accessible subway stations in reach. Their elevators worked ${(100 * p.a_uptime).toFixed(0)}% of the time.</p><p class="verdict">On the map</p></article>
    <article class="listing bad"><h3>${esc(p.b_name)} <span>${money(p.b_rent)}</span></h3><p>No accessible subway station. An accessible Uber takes ${p.b_wav.toFixed(0)} minutes to arrive — 1 time in 10, over ${p.b_wav_p90.toFixed(0)}.</p><p class="verdict">Off the map</p></article>`;
});
