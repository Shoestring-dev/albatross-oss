"use strict";
// Cycle gauge on the BTC and ETH coin pages: the MVRV Z-score's whole history from /api/onchain/mvrv, drawn
// with app.js's drawChart, with the levels past cycles turned at. Uses app.js helpers ($, esc, get, drawChart, state).
(() => {
  const last = {};     // asset -> the last answer, so the minute refresh redraws at once (no 86px jump)
  async function gauge(sym) {
    const asset = { BTC: "btc", ETH: "eth" }[sym];
    if (!asset || !$("#coin-body")) return;
    if (last[asset]) render(sym, last[asset]);
    let d;
    try { d = await get(`/api/onchain/mvrv?asset=${asset}`); } catch { return; }
    if (state.coin !== sym || !d.points.length) return;
    const same = last[asset] && last[asset].latest.date === d.latest.date && $("#coin-body .mvrv-panel");
    last[asset] = d;
    if (!same) render(sym, d);
  }
  function render(sym, d) {
    const body = $("#coin-body");
    if (!body) return;
    body.querySelector(".mvrv-panel")?.remove();
    const z = d.latest.z;
    const panel = document.createElement("article");
    panel.className = "panel mvrv-panel";
    panel.innerHTML = `<header class="panel-h"><h2>Cycle gauge</h2><span class="cat">MVRV Z-score, weekly since ${esc(d.points[0][0].slice(0, 4))} · Coin Metrics</span></header>
      <p class="mvrv-now"><b>${z.toFixed(2)}</b> on ${esc(d.latest.date)}. Market value against realized value (what holders paid), in standard deviations.
        It works on months, not days: below 0 has marked cycle bottoms, the top lines past peaks.</p>
      <div class="mvrv-chart"></div>`;
    const grid = body.querySelector(".coin-grid");
    grid ? grid.after(panel) : body.append(panel);
    drawChart(panel.querySelector(".mvrv-chart"), d.points.map(([t, v]) => ({ t: new Date(`${t}T00:00:00Z`), v })), d.marks,
      { fmt: (v) => v.toFixed(1), label: "Week of", aria: `${sym} MVRV Z-score since ${d.points[0][0].slice(0, 4)}`,
        dateFmt: (t) => t.toISOString().slice(0, 10) });
  }
  document.addEventListener("coinloaded", (e) => gauge(e.detail.symbol));
})();
