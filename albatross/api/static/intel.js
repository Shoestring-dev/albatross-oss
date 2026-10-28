"use strict";
// Where the edge is: the factor rank (the one signal with measured edge), every source's record on
// direction and on coin picking, and what stands out now, from /api/intel. Self-contained: it adds a
// panel to Today, a rank line to the coin page and the source records to Track record, using app.js
// helpers ($, esc, get, openAsset, state).
(() => {
  let I = null;
  const STATUS = { proven: "proven", worse: "worse", none: "none", early: "early" };
  const badge = (status, words) => `<span class="ev-badge ${STATUS[status] || "early"}">${esc(words)}</span>`;
  const chip = (r, tone) => `<button class="ev-coin ${tone}" data-sym="${esc(r.symbol)}" title="Open ${esc(r.symbol)}">
      <b>${esc(r.symbol)}</b><span class="ev-rank">#${r.rank}</span>
      <span class="ev-why">${esc((r.reasons || []).join(" · ") || "ranked on the three factors")}</span></button>`;

  function recordLine(rec) {
    const b = rec?.backtest, l = rec?.live;
    const hist = b?.weeks ? `History: over ${b.weeks} weeks the strongest fifth beat the weakest in ${b.won_pct}% of weeks, by ${b.mean_spread_pct > 0 ? "+" : ""}${b.mean_spread_pct}% a week on average (before costs).` : "";
    const live = l?.weeks ? ` Live: ${l.weeks} week${l.weeks === 1 ? "" : "s"}, ${l.mean_spread_pct > 0 ? "+" : ""}${l.mean_spread_pct}% a week.`
      : rec?.live_since ? ` Live since ${rec.live_since}: the first live week is graded 7 days after it starts.` : "";
    return hist + live;
  }

  function renderToday() {
    const host = $("#home-intel");
    if (!host || !I) return;
    const r = I.ranking;
    if (!r) { host.innerHTML = `<p class="note">The first ranking is written at 00:30 UTC.</p>`; return; }
    const proven = I.edge.filter((e) => e.status === "proven" || e.picking?.status === "proven");
    host.innerHTML = `
      <header class="panel-h"><h2>Factor rank</h2><span class="cat">${esc(r.n)} liquid coins · ${esc(r.day)}</span></header>
      <p class="ev-lede">Coins that are calm, near their 20-day high and trading unusual volume beat the rest in the back-test. This ranks coins against each other; nothing we track predicts where the whole market goes. It was chosen from about ten factors tried on the same history, so only its live weeks count.</p>
      <div class="ev-cols">
        <div><h3 class="ev-h up">Strongest</h3><div class="ev-list">${r.strongest.map((x) => chip(x, "up")).join("")}</div></div>
        <div><h3 class="ev-h down">Weakest</h3><div class="ev-list">${r.weakest.map((x) => chip(x, "down")).join("")}</div></div>
      </div>
      <p class="ev-foot">${esc(recordLine(I.record))} ${proven.length ? `Proven sources: ${esc(proven.map((e) => e.name).join(", "))}.`
        : "No AI source has proven an edge yet; their calls are opinions until the record says otherwise."}
        <a href="#/scorecard" data-go="scorecard">See every source's record →</a></p>`;
  }

  function renderCoinRank(sym) {
    const body = $("#coin-body");
    if (!body || !I?.ranking) return;
    body.querySelector(".ev-coinline")?.remove();
    const row = (I.ranking.all || {})[sym];
    const div = document.createElement("div");
    div.className = "panel ev-coinline";
    div.innerHTML = row
      ? `<b>Factor rank ${row.rank} of ${I.ranking.n}</b> · ${row.quintile >= 4 ? "strong" : row.quintile <= 2 ? "weak" : "middle of the pack"}${(row.reasons || []).length ? ` · ${esc(row.reasons.join(" · "))}` : ""}
         <span class="cat">Factor rank ranks liquid coins against each other. Its back-test was positive after costs; only its live weeks count.</span>`
      : `<span class="cat">Not ranked: the factor rank covers coins trading over $5M a day on Hyperliquid.</span>`;
    body.prepend(div);
  }

  // Track record already answers "which sources beat a naive caller?" (direction). Add the other half
  // under it: does each source pick coins that beat the rest, and the factor rank's own record.
  function renderScorecard() {
    const host = $("#ans-sources");
    if (!host || !I) return;
    let box = $("#ev-picking");
    if (!box) { box = document.createElement("div"); box.id = "ev-picking"; box.className = "ev-picking"; host.append(box); }
    box.innerHTML = `<h3 class="ev-sub">Picking coins that beat the rest</h3>
      <p class="cat">Direction is close to unpredictable, so each source is also graded on whether its bullish coins beat its bearish ones in the same run.</p>
      <div class="ev-pick-list">${I.edge.map((e) => `<span class="ev-pick"><b>${esc(e.name)}</b> ${e.picking ? badge(e.picking.status, e.picking.words) : badge("early", "too early to tell")}</span>`).join("")}</div>
      <p class="ev-foot"><b>Factor rank:</b> ${esc(recordLine(I.record))}</p>`;
  }

  // Guarded like every loader in app.js: loading and failure show the shared states, with a retry.
  // Track record: Jev's paper book and whether its probabilities mean what they say (/api/jev).
  let J = null;
  function renderJev() {
    const host = $("#ans-ideas");
    if (!host || !J) return;
    let box = $("#jev-scout");
    if (!box) { box = document.createElement("div"); box.id = "jev-scout"; box.className = "ev-picking"; host.append(box); }
    const b = J.book, c = J.counts, pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
    const rows = J.calibration.filter((x) => x.n).map((x) => `<tr><td class="left">${Math.round(x.from * 100)}–${Math.round(x.to * 100)}%</td>
      <td>${pct(x.won)}</td><td>${x.n.toLocaleString()}</td><td>${x.avg_r == null ? "–" : `${x.avg_r > 0 ? "+" : ""}${x.avg_r.toFixed(2)}R`}</td></tr>`).join("");
    // The correction (jev_calibration): per timeframe, what Jev said, what won, what its 40% now reads as, and the
    // check that matters, candidates scored with the corrected number and graded since.
    const K = J.correction;
    const fix = K ? K.by_tf.map((t) => `<tr><td class="left">${esc(t.tf)}</td><td>${t.n.toLocaleString()}</td><td>${pct(t.said)}</td><td>${pct(t.won)}</td>
      <td>${pct(t.reads_40)}${t.map && t.map !== t.tf ? ` <span class="cat">(${t.map === "all" ? "all" : esc(t.map)})</span>` : ""}</td>
      <td>${t.n_cal ? `${pct(t.cal)} → ${pct(t.cal_won)} <span class="cat">(${t.n_cal.toLocaleString()})</span>` : "–"}</td></tr>`).join("") : "";
    box.innerHTML = `<h3 class="ev-sub">Jev trade scout (paper)</h3>
      <p class="cat">Every 5 minutes TypeSafe's Jev scores a long and a short on each coin and timeframe (5m to 1d), built from our own work
        (stop 1.5 ATR, target 2R, 6 candles). The best go into a paper book: Jev's probability, corrected against its own record, at least ${pct(J.rules.min_p)},
        and expected at least +${J.rules.min_ev}R after that coin's trading costs, at most ${J.rules.max_open} open. Nothing is traded.</p>
      <p class="ev-foot"><b>Paper book:</b> ${b.open ?? 0} open, ${b.closed ?? 0} closed${b.closed ? `; ${pct(b.won)} reached the target, average ${(b.avg_r ?? 0) > 0 ? "+" : ""}${(b.avg_r ?? 0).toFixed(2)}R, total ${(b.total_r ?? 0).toFixed(1)}R after costs` : ""}.
        <b>Scored:</b> ${(c.scored ?? 0).toLocaleString()} candidates, ${(c.graded ?? 0).toLocaleString()} graded so far.</p>
      ${rows ? `<div class="table-wrap bare"><table class="plain"><thead><tr><th class="left">Jev said</th><th>Actually won</th><th>Candidates</th><th>Average</th></tr></thead>
        <tbody>${rows}</tbody></table></div><p class="cat">If Jev's probabilities meant what they say, the second column would track the first.</p>`
        : `<p class="cat">Calibration fills in as candidates reach their horizon: 90 minutes for 15m trades, up to 6 days for 1d.</p>`}
      ${fix ? `<h3 class="ev-sub">Jev's odds, corrected</h3>
        <p class="cat">Jev ranks trades in the right order but states its odds 2–3 times too high, so before a probability counts it is read against Jev's own
          graded record for that timeframe, refit every 10 minutes${K.fitted_at ? ` (last ${ago(K.fitted_at)})` : ""}. Recent grades count most, because how often
          targets are hit swings with the market within hours: ${K.half_life_h ? `5m and 15m remember about ${K.half_life_h["15m"]} hours, 1h and up about
          ${Math.round(K.half_life_h["1h"] / 24)} days` : "the last few hours weigh most"}. A timeframe with too few grades borrows the nearest one's (in brackets).
          Expected R and the paper book use the corrected number.</p>
        <div class="table-wrap bare"><table class="plain"><thead><tr><th class="left">Timeframe</th><th>Graded</th><th>Jev said</th><th>Won</th>
          <th>Its 40% reads as</th><th>Corrected → won</th></tr></thead><tbody>${fix}</tbody></table></div>
        <p class="cat">The last column is the real test: candidates scored with the corrected number and graded since. Its two figures should match.</p>` : ""}`;
  }
  const loadJev = async () => { try { J = await get("/api/jev"); if (J) renderJev(); } catch (e) { console.error(e); } };
  document.addEventListener("viewchange", (e) => { if (e.detail.view === "scorecard") loadJev(); });
  document.addEventListener("refresh", (e) => { if (e.detail.view === "scorecard") loadJev(); });
  if (state.tab === "scorecard") loadJev();

  const load = guarded("the edge ranking", ["#home-intel"], async () => {
    I = await get("/api/intel");
    renderToday(); renderScorecard();
    if (state.tab === "coin" && state.coin && state.coin !== missing) renderCoinRank(state.coin);
  });

  document.addEventListener("click", (e) => {
    const b = e.target.closest(".ev-coin[data-sym]");
    if (b) openAsset(b.dataset.sym);
  });
  // An unknown symbol gets no rank line: "not ranked" would suggest it is a coin we track.
  let missing = null;
  document.addEventListener("coinloaded", (e) => { missing = e.detail.missing ? e.detail.symbol : null; if (!missing) renderCoinRank(e.detail.symbol); });
  document.addEventListener("viewchange", (e) => { if (e.detail.view === "scorecard") renderScorecard(); });
  load();
  setInterval(load, 300_000);
  document.addEventListener("refresh", (e) => { if (e.detail.view === "home" && !I) load(); });   // failed: try again on the minute tick
})();
