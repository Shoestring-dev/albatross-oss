"use strict";
// Calls › Paper books (albatross/backtest/books.py over /api/books): what following each kind of call would have made
// or lost, in dollars after every cost, and whether the Deep End's dossier picks winners. Paper only: nothing trades.
// Uses app.js helpers: $, esc, get, ago, guarded, niceTicks, showTip, hideTip, row.
(() => {
  const tab = $("#tab-books");
  if (!tab) return;
  const S = { data: null };
  const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
  // a value shown to d decimals with the sign of what is shown: one that rounds to zero has none (never "−0.00")
  const fixed = (v, d) => { const a = Math.abs(v).toFixed(d); return `${+a === 0 ? "" : sign(v)}${a}`; };
  const usd = (v, signed = true) => (v == null ? "–"
    : `${signed ? sign(v) : v < 0 ? "−" : ""}$${Math.abs(v) >= 100 ? Math.round(Math.abs(v)).toLocaleString() : Math.abs(v).toFixed(2)}`);
  const pct = (v, d = 1) => (v == null ? "–" : `${fixed(v, d)}%`);
  const rr = (v) => (v == null ? "–" : `${fixed(v, 2)}R`);
  const share = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
  const int = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const day = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US",
    { month: "short", day: "numeric", year: String(d).slice(0, 4) === String(new Date().getUTCFullYear()) ? undefined : "numeric", timeZone: "UTC" }) : "–");
  const tone = (v) => (v == null || v === 0 ? "" : v > 0 ? "up" : "down");
  const STATE = {
    too_early: ["Too early", "muted"], making: ["Making money", "good"], losing: ["Losing money", "critical"],
    unclear: ["Can't tell yet", "warning"], helps: ["Marks losers", "good"], hurts: ["Marks winners", "critical"],
    audit: ["Under audit", "warning"],
  };
  const chip = (v) => { const [w, t] = STATE[v.status] || [v.status, "muted"]; return `<span class="jstate ${t}">${esc(w)}</span>`; };
  // The fade book's entries sit at the candle close, about 8 minutes before its calls exist (the CEO memo's audit):
  // no one could have had those fills, so its verdict is withheld until the timestamp audit is done.
  const AUDIT = new Set(["fade_quant"]);
  const bookChip = (b) => chip(AUDIT.has(b.key) ? { status: "audit" } : b.verdict);
  const ci = (i, fmt) => (i ? `${fmt(i.value)} <span class="sub">(95% interval ${fmt(i.lo)} to ${fmt(i.hi)})</span>` : "–");
  // Days are the samples on a card: with under a week of them an interval says nothing yet.
  const ciDays = (i, fmt) => (!i ? "–" : i.clusters < 7 ? `${fmt(i.value)} so far <span class="sub">(an interval needs a week of days)</span>` : ci(i, fmt));
  const pts = (v) => `${fixed(v, 1)} points`;
  // a flag's difference comes as failed minus the rest; it reads as passed minus failed, like the red-flag split
  const flip = (i) => (i ? { ...i, value: -i.value, lo: -i.hi, hi: -i.lo } : i);
  const coinLink = (coin, symbol, lane) => {
    if (lane === "majors") return `<a href="#/coin/${encodeURIComponent(coin)}">${esc(coin)}</a>`;
    const [chain, address] = String(coin).split(":");
    const label = symbol || `${address.slice(0, 4)}…${address.slice(-4)}`;
    return `<a href="#/deep/${encodeURIComponent(chain)}/${encodeURIComponent(address)}">${esc(label)}</a>`;
  };

  async function load() {
    S.data = await get("/api/books");
    render();
  }

  // One chart: the book's line, and a dashed comparison (the baseline book, or every ranked coin), on one time axis.
  // ref: the level drawn as the axis' anchor (0 for P&L, the starting capital for equity).
  function chart(host, main, other, opt) {
    const P = (main || []).map(([t, v]) => ({ t: +new Date(t), v })), Q = (other || []).map(([t, v]) => ({ t: +new Date(t), v }));
    if (P.length < 2) { host.innerHTML = `<p class="note bk-empty">${esc(opt.empty)}</p>`; return; }
    const W = Math.max(host.clientWidth || 520, 260), H = Number(host.dataset.h) || 150, L = 6, T = 8, B = 20;
    const t0 = Math.min(P[0].t, Q.length ? Q[0].t : Infinity), t1 = Math.max(P[P.length - 1].t, Q.length ? Q[Q.length - 1].t : -Infinity, opt.now || 0);
    const vals = [...P, ...Q].map((p) => p.v).concat(opt.ref);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.08;
    lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, 3);
    // the right gutter fits the longest tick label ("−$20,000" needs more than "$0")
    const R = Math.max(46, Math.ceil(Math.max(...ticks.map((v) => String(opt.tick(v)).length)) * 7.4) + 12);   // 12px on a phone
    const x = (t) => L + ((t - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    // Realized P&L moves in steps (flat until the next sale, then up or down) and holds to now; equity is marked daily.
    const path = (s) => s.map((p, i) => (!i ? `M${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`
      : opt.step ? `H${x(p.t).toFixed(1)}V${y(p.v).toFixed(1)}` : `L${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`)).join("")
      + (opt.step && opt.now ? `H${x(opt.now).toFixed(1)}` : "");
    const when = (t) => new Date(t).toLocaleString("en-US", { month: "short", day: "numeric", ...(opt.daily ? {} : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }), timeZone: "UTC" });
    host.innerHTML = `<svg class="chart bk-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="height:${H}px" role="img" aria-label="${esc(opt.aria)}">
      ${ticks.map((v) => `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-label" x="${W - R + 6}" y="${y(v) + 4}">${esc(opt.tick(v))}</text>`).join("")}
      <line class="bk-ref" x1="${L}" x2="${W - R}" y1="${y(opt.ref)}" y2="${y(opt.ref)}"/>
      <text class="axis-label" x="${L}" y="${H - 5}">${esc(when(t0))}</text>
      <text class="axis-label" x="${W - R}" y="${H - 5}" text-anchor="end">${esc(when(t1))} UTC</text>
      ${Q.length > 1 ? `<path class="bk-other" d="${path(Q)}"/>` : ""}<path class="series bk-main ${tone(P[P.length - 1].v - opt.ref)}" d="${path(P)}"/>
      <line class="cross" y1="${T}" y2="${H - B}" visibility="hidden"/><circle class="dot" r="4" visibility="hidden"/>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="bk-hit"/></svg>`;
    const svg = host.querySelector("svg"), cross = svg.querySelector(".cross"), dot = svg.querySelector(".dot");
    const at = (s, t) => { let v = null; for (const p of s) { if (p.t > t) break; v = p.v; } return v; };
    const hit = svg.querySelector(".bk-hit");
    const show = (e) => {
      const r = svg.getBoundingClientRect(), t = t0 + (((e.clientX - r.left) / r.width) * W - L) / (W - L - R) * (t1 - t0);
      const v = at(P, t);
      if (v == null) { hideTip(); return; }
      cross.setAttribute("x1", x(t)); cross.setAttribute("x2", x(t)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(t)); dot.setAttribute("cy", y(v)); dot.setAttribute("visibility", "visible");
      const w = Q.length ? at(Q, t) : null;
      showTip(e, `<b>${esc(opt.value(v))}</b>${row(opt.mainLabel, when(t) + " UTC")}${w == null ? "" : row(opt.otherLabel, opt.value(w))}`);
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);                // a tap on a phone
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function tradeCard(b) {
    if (b.starts) {             // set after seeing results: nothing to show until its start
      return `<article class="panel bk-card" id="bk-${esc(b.key)}">
        <header class="panel-h"><h2>${esc(b.name)}</h2>${bookChip(b)}</header>
        <p class="bk-rule">${esc(b.rule)}</p><p class="bk-verdict">${esc(b.verdict.words)}</p></article>`;
    }
    const v = b.unit === "R" ? rr : (x) => pct(x);
    const spent = b.unit === "R" ? `risked on ${int(b.closed)} closed ${b.closed === 1 ? "idea" : "ideas"}`
      : `staked on ${int(b.closed)} ${b.closed === 1 ? "trade" : "trades"}${b.hold ? ` whose ${b.hold} are up` : " that closed"}`;
    const openLine = b.open ? `In all, with the ${int(b.open)} still open sold now: <b class="${tone(b.now_pnl_usd)}">${usd(b.now_pnl_usd)}</b>`
      + (b.open_marked < b.open ? ` <span class="sub">(${int(b.open_marked)} of ${int(b.open)} have a price yet)</span>` : "") : "";
    const young = b.open > b.closed;          // most of the book is still open: the total matters as much as the counted part
    const skipped = b.skipped || {};
    const notes = [skipped.holding && `${int(skipped.holding)} skipped while the coin was still held`,
      skipped.no_dossier && `${int(skipped.no_dossier)} calls had no scored dossier when they came in`,
      skipped.unpriced && `${int(skipped.unpriced)} couldn't be graded`,
      skipped.not_listed && `${int(skipped.not_listed)} calls were on coins Hyperliquid doesn't list, so they're left out`,
      b.stalled && `${int(b.stalled)} counted at their last price because our candles for the coin stopped before the 24 hours were up (it stopped trading, or our price source failed)`,
      b.never_priced && `${int(b.never_priced)} ${b.key === "bot" ? "entries" : "calls"} in the same period never got a price within 10 minutes of the call, so no book has them`].filter(Boolean);
    const list = (xs) => xs.map((t) => `<li>${coinLink(t.coin, t.symbol, b.lane)} <span class="sub">${esc(day(t.at))}</span> <b class="${tone(t.value)}">${esc(v(t.value))}</b></li>`).join("");
    return `<article class="panel bk-card" id="bk-${esc(b.key)}">
      <header class="panel-h"><h2>${esc(b.name)}</h2>${bookChip(b)}</header>
      <p class="bk-rule">${esc(b.rule)}</p>
      <p class="bk-big"><b class="${tone(b.pnl_usd)}">${usd(b.pnl_usd)}</b> <span>${esc(`${usd(b.staked_usd, false)} ${spent}`)}</span></p>
      ${openLine ? `<p class="bk-open${young ? " big" : ""}">${openLine}</p>` : ""}
      <div class="bk-chart" data-chart="${esc(b.key)}"></div>
      <dl class="bk-stats">
        <div><dt>Per ${b.unit === "R" ? "idea" : "trade"}</dt><dd>${esc(v(b.per_trade?.value))}</dd></div>
        <div><dt>Won</dt><dd>${share(b.win_rate)}</dd></div>
        <div><dt>Median</dt><dd>${esc(v(b.median))}</dd></div>
        <div><dt>Open</dt><dd>${int(b.open)}${b.open_marked ? ` <span class="sub">${usd(b.open_pnl_usd)} if sold now</span>` : ""}</dd></div>
        <div><dt>${b.unit === "R" ? "Most at risk" : "Most at work"}</dt><dd>${usd(b.max_in_use_usd, false)}</dd></div>
        <div><dt>Worst dip</dt><dd>${usd(b.max_drawdown_usd)} <span class="sub">${b.unit === "R" ? "closed ideas" : "counted trades"}</span></dd></div>
      </dl>
      ${b.vs_baseline ? `<p class="bk-vs">Per trade against ${esc(b.vs_baseline.against)}: ${ciDays(b.vs_baseline, pts)}</p>` : ""}
      <p class="bk-verdict">${esc(b.verdict.words)}</p>
      ${notes.length ? `<p class="bk-foot">${esc(cap(notes.join("; ")))}. Since ${esc(day(b.since))}.</p>` : `<p class="bk-foot">${b.since ? `Since ${esc(day(b.since))}.` : "No trade yet."}</p>`}
      ${b.best?.length ? `<details class="bk-trades"><summary>Best and worst</summary><div class="bk-tw">
        <ul>${list(b.best)}</ul><ul>${list(b.worst)}</ul></div></details>` : ""}
    </article>`;
  }

  // ─── Results › Books: one compact card per book; a click opens its page ───────
  const CMP = {
    "holding the same calls 24 hours": "holding the same calls for 24 hours",
    "every Telegram call since this book began": "every Telegram call",
    "every call that had a scored dossier since this book began": "every scored call",
    "every call that had a scored dossier, sold at 2x, since this book began": "scored calls sold at 2x",
    "the same side on an average coin the model called that hour, before costs": "the same side on an average coin, before costs",
  };
  function lean(b) {
    const vs = b.vs_baseline;
    if (b.key === "calls") return { note: "Baseline", cls: "base", text: "Selling at 2x, FOMO and the bot are compared with this book; the dossier rules with its scored calls" };
    if (AUDIT.has(b.key)) return { note: null, text: vs ? `${sign(vs.value)}${Math.abs(vs.value).toFixed(2)} points a trade against ${CMP[vs.against] || vs.against}; entries timed before the calls existed` : "Entries timed before the calls existed" };
    if (b.unit === "equity") return { note: null, text: b.benchmark_usd != null ? `${usd(b.pnl_usd)} against ${usd(b.benchmark_usd - b.capital_usd)} for every ranked coin; no range until week 6` : "No comparison for a long-and-short book" };
    if (!vs || vs.value == null) return { note: null, text: "No comparison set yet" };
    const ahead = vs.lo > 0, behind = vs.hi < 0;
    return { note: ahead ? "Ahead" : behind ? "Behind" : "Level", cls: ahead ? "ahead" : behind ? "behind" : "level",
      text: `${Math.abs(vs.value).toFixed(1)} points a trade ${vs.value > 0 ? "better" : "worse"} than ${CMP[vs.against] || vs.against} · range ${sign(vs.lo)}${Math.abs(vs.lo).toFixed(1)} to ${sign(vs.hi)}${Math.abs(vs.hi).toFixed(1)}` };
  }
  function dueChip(b) {
    if (AUDIT.has(b.key) || b.verdict.status !== "too_early") return bookChip(b);
    const parts = [...(b.verdict.words || "").matchAll(/([\d,]+) of the ([\d,]+) (?:counted |closed )?(trades|ideas|days|weeks) needed/g)];
    const n = (x) => Number(x.replace(/,/g, ""));
    const frac = parts.length ? Math.min(...parts.map((m) => n(m[1]) / n(m[2]))) : 1;
    return `<span class="bk-due" title="${esc(b.verdict.words)}"><span class="jstate muted">Too early</span><b>${esc(day(b.verdict.first_read))}</b>
      <span class="vbar"><i style="width:${Math.max(4, Math.round(frac * 100))}%"></i></span></span>`;
  }
  function miniCard(b, all) {
    if (b.starts) return tradeCard(b);
    const r = b.unit === "R", eq = b.unit === "equity";
    const per = eq ? "" : ` · ${r ? rr(b.per_trade?.value) : pct(b.per_trade?.value)} ${r ? "an idea" : "a trade"}`;
    const count = eq ? `on ${usd(b.capital_usd, false)} since ${day(b.since)}` : `${int(b.closed)} ${r ? "ideas" : "trades"}`;
    const unit = r ? "ideas" : "trades";
    const open = eq ? "the last seven daily buys are still open" : !b.open ? "no open trades"
      : b.open_marked ? `${usd(b.now_pnl_usd)} with the ${int(b.open)} open ${unit} closed now` : `${int(b.open)} open ${unit} waiting for a price`;
    const c = lean(b), ls = all.find((x) => x.key === "factor");
    const foot = eq ? [["Weeks", `${b.weekly?.weeks || 0} of 6`], ["Every coin", b.benchmark_usd != null ? usd(b.benchmark_usd - b.capital_usd) : "–"],
      ["With shorts", ls && ls !== b ? usd(ls.pnl_usd) : "–"]]
      : [["Won", share(b.win_rate)], ["Worst dip", usd(b.max_drawdown_usd)], [r ? "Ideas" : "Trades", int(b.closed)]];
    return `<article class="panel bk-mini${AUDIT.has(b.key) ? " audit" : ""}" data-book="${esc(b.key)}" tabindex="0" role="link" aria-label="Open ${esc(b.name)}">
      <header class="bk-mh"><h2>${esc(b.name)}</h2>${dueChip(b)}</header>
      <p class="bk-rule">${esc(b.rule)}</p>
      <div class="bk-big"><b class="${tone(b.pnl_usd)}">${usd(b.pnl_usd)}</b><span>${esc(count + per)}</span><span class="bk-openl">${esc(open)}</span></div>
      <div class="bk-chart" data-chart="${esc(b.key)}" data-h="104"></div>
      <p class="bk-cmp${c.note ? "" : " nochip"}">${c.note ? `<span class="lean ${c.cls}">${esc(c.note)}</span>` : ""}<span>${esc(c.text)}</span></p>
      <dl class="bk-f3">${foot.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>
    </article>`;
  }

  function factorCard(b) {
    const h = b.history || {}, w = b.weekly || {}, long = b.key === "factor_long";
    const hist = h.weeks ? (long
      ? `The same rules on the history, ${day(h.from)} to ${day(h.to)}: ${pct(h.return_pct)}, while the same slices in every ranked coin made ${pct(h.benchmark_return_pct)}; ahead by ${pct(h.mean_pct, 2)} a week on average (t ${h.t ?? "–"}, ${h.up_pct}% of ${h.weeks} weeks).`
      : `The same rules on the history, ${day(h.from)} to ${day(h.to)}: ${pct(h.return_pct)}, ${pct(h.mean_pct, 2)} a week on average (t ${h.t ?? "–"}, up in ${h.up_pct}% of ${h.weeks} weeks).`) : "";
    return `<article class="panel bk-card" id="bk-${esc(b.key)}">
      <header class="panel-h"><h2>${esc(b.name)}</h2>${bookChip(b)}</header>
      <p class="bk-rule">${esc(b.rule)}</p>
      <p class="bk-big"><b class="${tone(b.pnl_usd)}">${usd(b.pnl_usd)}</b> <span>${esc(`on ${usd(b.capital_usd, false)}, ${b.days} ${b.days === 1 ? "day" : "days"} live`)}${long && b.benchmark_usd != null ? `; every coin ${esc(usd(b.benchmark_usd - b.capital_usd))}` : ""}</span></p>
      <div class="bk-chart" data-chart="${esc(b.key)}"></div>
      ${w.weeks ? `<p class="bk-vs">${long ? "Ahead of every coin by" : "Making"} ${pct(w.mean_pct, 2)} a week over ${w.weeks} ${w.weeks === 1 ? "week" : "weeks"} (t ${w.t ?? "–"}).</p>` : ""}
      <p class="bk-verdict">${esc(b.verdict.words)}</p>
      ${hist ? `<p class="bk-foot">${esc(hist)} Chosen after trying about ten factors, with no funding before Sep 29 and the ranked day's close as the price before March 2026, so expect less live.</p>` : ""}
    </article>`;
  }

  function drawCharts(books) {
    const by = Object.fromEntries(books.map((b) => [b.key, b]));
    const now = S.data?.books?.computed_at ? +new Date(S.data.books.computed_at) : Date.now();
    document.querySelectorAll("#tab-books [data-chart], #tab-book [data-chart]").forEach((host) => {
      const b = by[host.dataset.chart];
      if (!b) return;
      if (b.unit === "equity") {
        chart(host, b.curve, b.benchmark_curve, { ref: b.capital_usd, daily: true, tick: (v) => `$${Math.round(v).toLocaleString()}`,
          value: (v) => usd(v, false), mainLabel: "Book", otherLabel: "Every coin", aria: `${b.name}: equity by day`,
          empty: "The line starts after the second daily close." });
      } else {          // alone on its own scale: another book's dollars come from a different number of trades
        chart(host, b.curve, null, { ref: 0, now, step: true, tick: (v) => `${sign(v)}$${Math.round(Math.abs(v)).toLocaleString()}`, value: (v) => usd(v),
          mainLabel: "Realized", otherLabel: "", aria: `${b.name}: realized profit and loss`,
          empty: b.closed ? "One sale so far: the line starts with the next." : "No trade has closed yet." });
      }
    });
  }

  function group(x, label) {
    return `<div class="bk-side"><h4>${esc(label)}</h4><p><b class="${tone(x.mean_pct)}">${pct(x.mean_pct)}</b> a call on average</p>
      <p class="sub">${pct(x.median_pct)} median · ${share(x.up)} up after 24 h · ${share(x.sold_2x)} reached 2x · ${int(x.n)} calls on ${int(x.coins)} coins</p></div>`;
  }

  function dossier(d) {
    const box = $("#bk-dossier");
    if (!d) { box.innerHTML = `<p class="note">No report yet: it is rebuilt every 15 minutes.</p>`; return; }
    const p = d.population, r = d.red, sc = d.score;
    const iv = (i, fmt) => (!i ? "–" : d.enough ? ci(i, fmt) : `${fmt(i.value)} so far <span class="sub">(the interval comes with the first read)</span>`);
    const bands = sc.bands.map((x) => `<tr><td class="left">${esc(x.label)}</td><td>${int(x.n)}</td><td>${int(x.coins)}</td>
      <td class="${tone(x.mean_pct)}">${pct(x.mean_pct)}</td><td>${pct(x.median_pct)}</td><td>${share(x.up)}</td><td>${share(x.sold_2x)}</td></tr>`).join("");
    const flags = d.flags.map((f) => `<tr><td class="left">${esc(f.label)}</td><td>${int(f.n)}</td><td class="${tone(f.mean_pct)}">${pct(f.mean_pct)}</td>
      <td>${pct(f.others_mean_pct)}</td><td>${f.diff ? iv(flip(f.diff), pts) : `<span class="cat">fewer than ${d.min.group}</span>`}</td></tr>`).join("");
    const cell = (x) => (x.n ? `<span class="${tone(x.mean_pct)}">${pct(x.mean_pct)}</span> <span class="sub">${int(x.n)}</span>` : `<span class="cat">–</span>`);
    const lines = d.lines.map((l) => `<tr><td class="left">${esc(l.label)}</td>${["good", "warn", "bad", "unknown"].map((s) => `<td>${cell(l.by[s])}</td>`).join("")}</tr>`).join("");
    box.innerHTML = `<p class="answer-verdict">${chip(d.verdict)} ${esc(d.verdict.words)}</p>
      <p class="bk-foot">Two questions were set in advance, below. Everything in the fold under them is exploratory.
        ${int(p.scored)} of ${int(p.graded)} graded Telegram calls had a scored dossier when they came in (${int(p.coins)} coins${p.since ? `, since ${esc(day(p.since))}` : ""});
        each is the $100 trade sold after 24 hours, all costs in.</p>
      <h4 class="answer-src">1 · Red flags</h4>
      <div class="bk-sides">${group(r.none, "Nothing flagged")}${group(r.any, "A red flag")}</div>
      <p class="bk-vs">Calls with nothing flagged minus calls with a red flag: ${iv(r.diff, pts)}</p>
      <h4 class="answer-src">2 · The score</h4>
      <p class="bk-vs">Rank correlation of the score with the 24-hour return: ${iv(sc.rho, (v) => fixed(v, 2))}</p>
      <details class="fold bk-fold"><summary>Score bands, each red flag, and every line by its status <span class="cat">exploratory</span></summary>
        <div class="table-wrap"><table class="plain bk-table"><thead><tr><th class="left">Score at the call</th><th>Calls</th><th>Coins</th><th>Average</th>
          <th>Median</th><th>Up after 24 h</th><th>Reached 2x</th></tr></thead><tbody>${bands}</tbody></table></div>
        ${flags ? `<div class="table-wrap"><table class="plain bk-table"><thead><tr><th class="left">Red flag line</th><th>Calls with it</th><th>Average with it</th>
          <th>Average without</th><th title="How much better every other call did than the calls where this check failed (calls it never ran on included): above zero, the flag marks losers">Without minus with</th></tr></thead><tbody>${flags}</tbody></table></div>` : `<p class="note">No call had a red-flag line yet.</p>`}
        <div class="table-wrap"><table class="plain bk-table"><thead><tr><th class="left">Line</th><th>Good</th><th>Warning</th><th>Bad</th><th>Unknown</th></tr></thead>
          <tbody>${lines}</tbody></table></div>
        <p class="note">Score bands, then each red-flag line, then every line: the average 24-hour return a call, and how many calls, for each status the line
          showed when the call came in. About ${int(d.comparisons)} comparisons are in this section; at that many, a few will look different by chance.</p>
      </details>`;
  }

  function render() {
    const d = S.data;
    if (!d?.books) {
      $("#bk-deep").innerHTML = `<p class="note">The books haven't been built yet: the job runs every 15 minutes.</p>`;
      $("#bk-majors").innerHTML = "";
      dossier(d?.dossier);
      return;
    }
    const books = d.books.books;
    $("#bk-when").textContent = `rebuilt ${ago(d.books.computed_at)}`;
    const ruled = books.filter((b) => b.verdict.status !== "too_early");
    const next = books.map((b) => b.verdict.first_read).filter(Boolean).sort()[0];
    const dossierRead = d.dossier?.verdict?.status === "too_early" ? d.dossier.verdict.first_read : null;
    const c = d.books.cost_24h;
    $("#bk-stands").innerHTML = `${ruled.length ? `${ruled.map((b) => `${esc(b.name)}: ${esc(STATE[b.verdict.status][0].toLowerCase())}`).join("; ")}.`
      : "No book has enough trades for a verdict yet."}${dossierRead ? ` The dossier report's first read is due around ${esc(day(dossierRead))}` : ""}${
      next ? `${dossierRead ? "; the books' first verdicts" : " The books' first verdicts are due"} around ${esc(day(next))}.` : dossierRead ? "." : ""}
      ${c ? ` Running the agents cost ${usd(c.api_usd, false)} in API calls and ${usd(c.pool_usd, false)} of subscription allowance in the last 24 hours (details on <a href="#/output">Spend &amp; allowance</a>); the data subscriptions and the server are not in that figure.` : ""}`;
    $("#bk-deep").innerHTML = books.filter((b) => b.lane === "deep").map((b) => miniCard(b, books)).join("");
    $("#bk-majors").innerHTML = books.filter((b) => b.lane === "majors").map((b) => miniCard(b, books)).join("");
    dossier(d.dossier);
    renderPage(books);
    drawCharts(books);
    // arriving from a Home standings row: that book's card, outlined
    if (state.bookFocus && tab.classList.contains("on")) {
      const card = document.getElementById(`bk-${state.bookFocus}`);
      state.bookFocus = null;
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "start" });
        card.classList.add("bk-focus");
        setTimeout(() => card.classList.remove("bk-focus"), 2400);
      }
    }
  }

  // ─── a book's page (#/book/<key>): the full card, wide, best and worst open ────
  function renderPage(books) {
    const host = $("#bk-page");
    if (!host || !state.book) return;
    const b = books.find((x) => x.key === state.book);
    if (!b) {
      host.innerHTML = `<p class="note">There's no book called "${esc(state.book)}". <a href="#/books" data-go="books">Every book →</a></p>`;
      document.dispatchEvent(new CustomEvent("bookloaded", { detail: { key: state.book, missing: true } }));
      return;
    }
    host.innerHTML = `<div class="bk-page">${(b.unit === "equity" ? factorCard(b) : tradeCard(b)).replace('<details class="bk-trades">', '<details class="bk-trades" open>')}</div>`;
    state.bookName = b.name;
    if (typeof renderSubnav === "function" && state.tab === "book") renderSubnav();
    document.dispatchEvent(new CustomEvent("bookloaded", { detail: { key: b.key, name: b.name, rule: b.rule } }));
  }
  const openBook = (key) => { state.book = key; setTab("book"); };
  tab.addEventListener("click", (e) => { const c = e.target.closest(".bk-mini[data-book]"); if (c && !e.target.closest("a")) openBook(c.dataset.book); });
  tab.addEventListener("keydown", (e) => {
    const c = e.target.closest(".bk-mini[data-book]");
    if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openBook(c.dataset.book); }
  });

  let resized;
  addEventListener("resize", () => { clearTimeout(resized); resized = setTimeout(() => { if (["books", "book"].includes(state.tab) && S.data?.books) drawCharts(S.data.books.books); }, 200); });
  const loadGuarded = guarded("the paper books", ["#bk-deep", "#bk-dossier", "#bk-page"], load);      // app.js: shared loading and failure states
  // the books and the dossier report (its own view under Results since the redesign) come from one answer
  const onView = (v) => { if (v === "books" || v === "dreport" || v === "book") loadGuarded().catch(console.error); };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => onView(e.detail.view));
  if (["#tab-books", "#tab-dreport", "#tab-book"].some((t) => $(t)?.classList.contains("on"))) loadGuarded().catch(console.error);
})();
