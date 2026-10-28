"use strict";
// Majors › Factor rank (the redesign, 2026-10-06): the liquid coins ranked against each other, the factor books'
// after-cost record with the reasons it can't settle anything yet, what the direction calls have earned (nothing
// beats a coin flip yet, so they sit under All coins and Sectors marked unproven) and, as context, what is unusual
// right now (crowded funding, volume shocks, fresh 20-day highs and lows).
// The market tiles and "what changed" above and beside it are gallery.js's (loadOverview). Uses app.js helpers.
(() => {
  const tab = $("#tab-factor");
  if (!tab) return;
  const F = { intel: null, books: null, open: false };
  const M = "−";
  const sgn = (v, d = 1) => `${v > 0 ? "+" : v < 0 ? M : ""}${Math.abs(v).toFixed(d)}`;
  const usd = (v) => `${v > 0 ? "+" : v < 0 ? M : ""}$${Math.round(Math.abs(v)).toLocaleString()}`;
  const day = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "–");

  function renderRank() {
    const r = F.intel?.ranking, host = $("#fr-rank");
    if (!r) { host.innerHTML = `<p class="note">The first ranking is written at 00:30 UTC.</p>`; return; }
    const all = Object.entries(r.all || {}).map(([sym, x]) => ({ sym, ...x })).sort((a, b) => a.rank - b.rank);
    const fifth = (q) => `<span class="fifth f${q}" role="img" aria-label="${q === 5 ? "strongest" : q === 1 ? "weakest" : `fifth ${q} of 5`}">${[1, 2, 3, 4, 5].map(() => "<i></i>").join("")}</span>`;
    const row = (x) => `<tr><td class="num">${x.rank}</td><td class="left"><button type="button" class="fr-coin" data-sym="${esc(x.sym)}">${esc(x.sym)}</button></td>
      <td>${fifth(x.quintile)}</td><td class="left fr-why">${esc((x.reasons || []).join(" · ") || "ranked on the three factors")}</td></tr>`;
    const top = all.slice(0, 8), bottom = all.slice(-8), middle = all.slice(8, -8);
    host.innerHTML = `<header class="panel-h"><h2>Factor rank</h2><span class="cat">${esc(day(r.day))} · ${all.length} liquid coins · strongest fifth at the top</span></header>
      <p class="ev-lede">Coins that are calm, near their 20-day high and trading unusual volume beat the rest in the back-test. The rank compares coins with each other; it says nothing about where the whole market goes.</p>
      <div class="table-wrap bare"><table class="plain fr-table"><thead><tr><th>#</th><th class="left">Coin</th><th>Fifth</th><th class="left">Why</th></tr></thead><tbody>
        ${top.map(row).join("")}
        ${middle.length ? (F.open ? middle.map(row).join("") : `<tr class="fr-gap"><td colspan="4"><button type="button" class="more-toggle" data-fr-open>Show ranks 9 to ${8 + middle.length}</button></td></tr>`) : ""}
        ${bottom.map(row).join("")}</tbody></table></div>`;
  }

  function renderRecord() {
    const host = $("#fr-record"), books = F.books?.books?.books || [];
    const fl = books.find((b) => b.key === "factor_long"), fs = books.find((b) => b.key === "factor");
    if (!fl) { host.innerHTML = ""; return; }
    const h = fl.history || {}, hs = fs?.history || {}, rec = F.intel?.record || {};
    const li = (k, v, tone = "") => `<li><span>${k}</span><b class="${tone}">${v}</b></li>`;
    // two records: the ranking itself (graded weekly from its live start, as on Sources) and the factor books that
    // trade it (their verdict counts its own weeks from the books' start)
    const lw = rec.live?.weeks || 0;
    host.innerHTML = `<header class="panel-h"><h2>Record</h2><span class="cat">${h.weeks ? `the books' back-test, ${h.weeks} weeks, after trading costs` : "live"}</span></header>
      <ul class="fr-facts">
        ${h.weeks ? li("Long only", `${sgn(h.return_pct)}%`, h.return_pct >= 0 ? "up" : "down") : ""}
        ${h.benchmark_return_pct != null ? li("Every ranked coin, same weeks", `${sgn(h.benchmark_return_pct)}%`, h.benchmark_return_pct >= 0 ? "up" : "down") : ""}
        ${h.weeks ? li("Long only ahead of every coin", `${h.up_pct}% of weeks · t ${h.t ?? "–"}`) : ""}
        ${hs.weeks ? li("Long and short", `${sgn(hs.return_pct)}% · t ${hs.t ?? "–"}`) : ""}
        ${rec.live_since ? li(`Ranking live since ${esc(day(rec.live_since))}`, lw ? `${lw} graded ${lw === 1 ? "week" : "weeks"}, ${sgn(rec.live.mean_spread_pct, 2)}% a week` : "first week not graded yet") : ""}
        ${li("Weeks counted for the books' verdict", `${fl.weekly?.weeks || 0} of 6`)}
        ${li(`Live long only since ${esc(day(fl.since))}, on $${(fl.capital_usd || 10000).toLocaleString()}`, usd(fl.pnl_usd), fl.pnl_usd >= 0 ? "up" : "down")}
        ${fl.benchmark_usd != null ? li("Every ranked coin, live", usd(fl.benchmark_usd - (fl.capital_usd || 10000)), fl.benchmark_usd >= (fl.capital_usd || 10000) ? "up" : "down") : ""}
        ${fs ? li("Long and short, live", usd(fs.pnl_usd), fs.pnl_usd >= 0 ? "up" : "down") : ""}
      </ul>
      <p class="note">Factor rank was chosen after trying about ten factors on this same history, and before March 2026 it is priced
        at the ranked day's close, so only the live weeks count. The back-tests leave funding out; the live books count it.
        A t below 2 is too weak to call. First live verdict ${esc(day(fl.verdict?.first_read))}.
        <a href="#/books" data-go="books">The factor books →</a></p>`;
  }

  function renderDirection() {
    const host = $("#fr-direction"), edge = F.intel?.edge || [];
    const cells = edge.flatMap((s) => s.tfs || []).map((t) => t.status);
    const n = (s) => cells.filter((x) => x === s).length, better = n("proven"), worse = n("worse"), none = n("none"), early = n("early");
    host.innerHTML = `<header class="panel-h"><h2>Direction calls</h2><span class="unproven">${better ? "Mixed" : "Unproven"}</span></header>
      <p class="fr-dir">${better ? `${better} source-and-timeframe ${better === 1 ? "cell beats" : "cells beat"} a one-way caller.`
        : "No direction source beats a one-way caller (always long or always short, whichever did better)."}
        Of ${worse + none + better} graded cells, ${worse} ${worse === 1 ? "is" : "are"} worse and ${none} ${none === 1 ? "is" : "are"} within noise; ${early} more are too early to grade.
        The coin table and the sector map show these calls, so read them as unproven.</p>
      <p class="fr-links"><a href="#/matrix" data-go="matrix">All coins →</a> <a href="#/sectors" data-go="sectors">Sectors →</a> <a href="#/scorecard" data-go="scorecard">Every source's record →</a></p>`;
  }

  function renderFunding() {
    const host = $("#fr-funding"), u = F.intel?.unusual || [];
    // api/intel.py _unusual: crowded funding first, then volume shocks and fresh 20-day highs and lows (12 at most)
    host.innerHTML = u.length ? `<header class="panel-h"><h2>Unusual right now</h2><span class="cat">context only</span></header>
      <ul class="fr-fund">${u.map((x) => `<li><button type="button" class="fr-coin" data-sym="${esc(x.symbol)}">${esc(x.symbol)}</button><span>${esc(String(x.text).replace(/(^|\s)-(\d)/g, `$1${M}$2`))}</span></li>`).join("")}</ul>` : "";
  }

  const render = () => { renderRank(); renderRecord(); renderDirection(); renderFunding(); };
  tab.addEventListener("click", (e) => {
    if (e.target.closest("[data-fr-open]")) { F.open = true; renderRank(); return; }
    const b = e.target.closest(".fr-coin[data-sym]");
    if (b) openAsset(b.dataset.sym);
  });
  const load = guarded("factor rank", ["#fr-rank"], async () => {
    const [intel, books] = await Promise.allSettled([get("/api/intel"), get("/api/books")]);
    if (intel.status === "rejected") throw intel.reason;
    F.intel = intel.value;
    if (books.status === "fulfilled") F.books = books.value;
    render();
  });
  document.addEventListener("viewchange", (e) => { if (e.detail.view === "factor") load(); });
  document.addEventListener("refresh", (e) => { if (e.detail.view === "factor") load(); });
  if (tab.classList.contains("on")) load();
})();
