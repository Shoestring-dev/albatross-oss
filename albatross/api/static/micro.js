"use strict";
// Micro-cap lane (docs/SPEC-MICROCAPS.md): the Micro-caps view, its token drawer, and the price-accuracy block on
// the Health tab. Kept out of app.js; uses its helpers ($, esc, get, row, tipId, tips, showTip, hideTip, ago, utc,
// fmtPct, setTab) and its drawer.
(() => {
  const PAGE = 50;
  const S = { chain: "", view: "active", q: "", sort: "activity", dir: -1, data: null, health: null, open: null,
    shown: PAGE, more: false, expanded: new Set(), error: null, sparks: {}, sparksAt: 0, sparksView: null, range: null };
  const CHAIN = { solana: "SOL", robinhood: "RH", bsc: "BSC", ethereum: "ETH", base: "BASE", evm: "EVM?" };
  const CHAIN_NAME = { solana: "Solana", robinhood: "Robinhood", bsc: "BNB Chain", ethereum: "Ethereum", base: "Base",
    evm: "Chain not found yet" };
  const EXPLORER = { solana: "solana", robinhood: "robinhood", bsc: "bsc", ethereum: "ethereum", base: "base" };
  const SRC = { jupiter: "Jupiter", dexscreener: "DexScreener", geckoterminal: "GeckoTerminal", consensus: "Consensus" };
  const TIER = { hot: "every 10 s (under an hour old, or just bought)", active: "every 30 s (under a day old, or held)",
    watch: "every 2 min", dormant: "hourly (no trading for a day)", retired: "no longer (retired)" };
  const DAY = 86_400_000;
  const tab = $("#tab-micro");

  // ─── formatting ──────────────────────────────────────────────────────────
  // Micro-cap prices: 0.0₄6894 = 0.00006894 (the subscript counts the zeros after the point).
  const SUB = "₀₁₂₃₄₅₆₇₈₉";
  function price(p) {
    if (p == null) return "–";
    if (p <= 0) return "0";
    if (p >= 1) return p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 0 : 3 });
    if (p >= 0.001) return p.toPrecision(4);
    const [m, e] = p.toExponential(3).split("e");
    return `0.0${String(-Number(e) - 1).replace(/\d/g, (d) => SUB[d])}${m.replace(".", "")}`;
  }
  const usd = (v) => (v == null ? "–" : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M`
    : v >= 1e4 ? `$${Math.round(v / 1e3)}K` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}K` : `$${Math.round(v)}`);
  const dur = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m`
    : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`);
  const short = (a) => (a.length > 14 ? `${a.slice(0, 5)}…${a.slice(-4)}` : a);
  const pctAbs = (v, d = 2) => (v == null ? "–" : `±${Number(v).toFixed(d)}%`);
  const share = (v) => (v == null ? "–" : `${(v * 100).toFixed(0)}%`);
  const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
  const list = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
  const key = (t) => `${t.chain}/${t.address}`;
  const name = (t) => t.symbol || short(t.address);
  const symKey = (t) => (t.symbol || "").trim().toLowerCase();
  const ms = (ts) => (ts ? +new Date(ts) : null);
  const lastActivity = (t) => Math.max(ms(t.last_call) || 0, ms(t.last_entry) || 0) || null;
  const flagged = (t, f) => (t.flags || []).includes(f);

  // What the price means, in one plain sentence (the drawer's headline and the rows' tooltips).
  function verdict(t) {
    const srcs = (t.sources || []).map((s) => SRC[s] || s);
    if (t.chain === "evm") return "The call named no chain, and the token isn't on Robinhood, Base, BNB Chain or Ethereum yet: no price until it is.";
    if (flagged(t, "rejected_print")) return "A jump of more than 10× seen by one source only is held back until a second source confirms it.";
    if (t.last_price_usd == null) return t.pool ? "No source has quoted its pool yet."
      : "No trading pool found where this token is the one being traded: no price until one appears.";
    if (t.stale) return `No fresh price: the last one is ${dur(t.price_age_s)} old, and a token this age needs one under ${dur(t.budget_s)}. It is shown dimmed, not as current.`;
    if (flagged(t, "disagree")) return `Sources disagree by ${pctAbs(t.spread_pct, 1)}: showing our own pool's price (${list(srcs)}).`;
    if (t.n_sources >= 2) return `Confirmed by ${list(srcs)}, within ${pctAbs(t.spread_pct, 1)} of each other, ${dur(t.price_age_s)} ago.`;
    return `Priced by ${list(srcs)} only (${t.pool ? "no second source covers this pool yet" : "no pool indexed yet to cross-check against"}), ${dur(t.price_age_s)} ago.`;
  }

  function checkBadge(t) {
    if (flagged(t, "rejected_print")) return `<span class="badge max">✕ held back</span>`;
    if (flagged(t, "disagree")) return `<span class="badge hi">⚠ disagree</span>`;
    if (t.stale && t.last_price_usd != null) return `<span class="mc-stale">stale</span>`;
    if (t.n_sources >= 2) return `<span class="badge ok">✓ ${t.n_sources} agree</span>`;
    if (t.n_sources === 1) return `<span class="badge">1 source</span>`;
    return `<span class="cat">no price</span>`;
  }

  // ─── the view ────────────────────────────────────────────────────────────
  const VIEWS = [
    ["active", "Active", "Priced, and called or bought by the bot in the last 24 hours (or still held)"],
    ["held", "Bot holds", "The bot holds an open real-money position"],
    ["new", "New", "First pool under an hour old"],
    ["all", "All", "Every token still watched"],
    ["attention", "Needs a look", "No pool, no fresh price, sources disagreeing, or a jump held back"],
    ["retired", "Retired", "No longer watched: 14 days old, or liquidity under $500 for a day"],
  ];
  const inView = {
    // A stale price stays in (dimmed): a new token priced by one slow source is often briefly stale.
    active: (t, now) => t.last_price_usd != null && (t.n_calls_24h > 0 || t.n_open > 0 || now - (ms(t.last_entry) || 0) < DAY),
    held: (t) => t.n_open > 0,
    new: (t) => t.age_s < 3600,
    all: () => true,
    attention: (t) => t.stale || !t.pool || flagged(t, "disagree") || flagged(t, "rejected_print"),
    retired: () => true,
  };

  tab.innerHTML = `
    <div class="kpis mc-kpis" id="mc-kpis"></div>
    <div class="filters mc-filters">
      <div class="chips" id="mc-view" role="group" aria-label="Which tokens">${VIEWS.map(([v, label, title]) =>
        `<button data-view="${v}" title="${esc(title)}"${v === S.view ? ` class="on"` : ""}>${label}<span class="mc-n"></span></button>`).join("")}</div>
      <select id="mc-chain" aria-label="Chain"><option value="">All chains</option></select>
      <input id="mc-search" type="search" placeholder="Symbol, name, address" aria-label="Search tokens">
      <button class="more-toggle" id="mc-more" aria-expanded="false">More columns</button>
    </div>
    <div class="mc-status" id="mc-status" role="status"></div>
    <div class="table-wrap"><table class="matrix mc-table" id="mc-table"></table></div>
    <div class="mc-foot" id="mc-foot"></div>
    <details class="note-fold"><summary>How prices work</summary>
      <p class="note">A token is priced only from a pool where it is the token being traded (never from a pool where it is the
      currency), the deepest such pool. The price is the median of the fresh quotes that agree within 5% (Jupiter,
      DexScreener, GeckoTerminal 1-minute candles). A price older than its token's limit (15 s under an hour old, 60 s under a
      day, 5 min after) is shown dimmed as <span class="mc-stale">stale</span>: unknown, never carried forward. Prices are
      shown with a subscript for leading zeros: 0.0₄68 = 0.000068.</p></details>
    <details class="fold" id="mc-acc-fold"><summary>How accurate are these prices? <span class="cat" id="mc-acc-sum"></span></summary>
      <div class="mc-acc"></div></details>`;

  async function load() {
    const view = S.view === "retired" ? "retired" : "live";
    if (!S.data) status(`<div class="load-state loading" role="status"><span class="msg">Loading the micro-caps…</span></div>`);
    try {
      const wantSparks = S.sparksView !== view || Date.now() - S.sparksAt > 60_000;
      const [d, h, sp] = await Promise.all([get(`/api/micro/tokens?status=${view}`), S.health ? null : get("/api/micro/health"),
        wantSparks ? get(`/api/micro/sparks?status=${view}`).catch(() => null) : null]);
      S.data = d;
      if (h) S.health = h;
      if (sp) Object.assign(S, { sparks: sp.sparks, sparksAt: Date.now(), sparksView: view });
      S.error = null;
    } catch (e) {
      S.error = whyFailed(e);        // app.js: the same words every other panel uses
    }
    render();
    renderAccuracy(tab.querySelector(".mc-acc"));
  }

  function status(html) { $("#mc-status").innerHTML = html; }
  // get() throws "<url>: <status>"; the status is what a reader needs.
  const errText = (e) => (/: (\d{3})$/.test(e.message) ? `server answered ${e.message.slice(-3)}` : e.message);

  function visible(tokens, now) {
    const q = S.q.trim().toLowerCase();
    return tokens.filter((t) => (!S.chain || t.chain === S.chain) && inView[S.view](t, now) && (!q
      || (t.symbol || "").toLowerCase().includes(q) || (t.name || "").toLowerCase().includes(q) || t.address.toLowerCase().includes(q)));
  }

  // ─── the call ────────────────────────────────────────────────────────────
  const livePx = (t) => t.price_usd ?? t.last_price_usd;
  const sinceCall = (t) => (t.call_price && livePx(t) ? (livePx(t) / t.call_price - 1) * 100 : null);
  const HOW = { pool: "the pool's last trade before the call", quote: "our own live quote at the call (the pool's candles hadn't been fetched that far yet)",
    ledger: "our price, taken {m} min after the call",
    after: "the first trade after the call ({m} min later)" };
  const howPriced = (src, after) => (HOW[src] || "").replace("{m}", after == null ? "?" : Math.max(1, Math.round(after)));
  const pctCls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
  const fmtSince = (v) => (v == null ? "–" : v >= 1000 ? `${(v / 100 + 1).toFixed(0)}×` : fmtPct(v, 0).replace("-", "−"));
  const xs = (v) => (v == null ? "–" : v >= 10 ? `${v.toFixed(0)}×` : `${v.toFixed(v >= 2 ? 1 : 2)}×`);

  function calledCell(t) {
    if (!t.call_price) return `<td class="cat" title="${t.call_at ? "No trade near the call to price it from" : "Never called: came in through a bot trade"}">–</td>`;
    const tip = `<b>${esc(name(t))}</b> · first called ${esc(ago(t.call_at))}` + row("Group", t.call_group || "–") + row("When", utc(t.call_at)) +
      row("Price then", price(t.call_price)) + `<div class="tip-text">Priced from ${esc(howPriced(t.call_price_src, t.call_price_after_min))}.</div>`;
    return `<td data-tipid="${tipId(tip)}">${price(t.call_price)}<span class="mc-sub">${ago(t.call_at)}</span></td>`;
  }

  function sinceCell(t) {
    const v = sinceCall(t);
    if (v == null) return `<td class="cat">–</td>`;
    return `<td><span class="mc-since ${pctCls(v)}">${fmtSince(v)}</span>${t.peak_x ? `<span class="mc-sub mc-peak" title="Highest trade since the call${t.peak_at ? `, ${esc(utc(t.peak_at))}` : ""}">peak ${xs(t.peak_x)}</span>` : ""}<span class="mc-spark-in">${spark(t)}</span></td>`;
  }

  // The list's small chart: closes since just before the first call (SPARK_POINTS buckets from /api/micro/sparks), the
  // call price as a dashed line and a dot where the call came in. Log scale once the range passes 3x.
  function spark(t) {
    const s = S.sparks[key(t)];
    if (!s || !t.call_price) return `<span class="cat">–</span>`;
    let last = s.before ?? s.v.find((v) => v != null);
    const vals = s.v.map((v) => (v == null ? last : (last = v)));
    const nums = [...vals.filter((v) => v != null), t.call_price];
    const lo = Math.min(...nums), hi = Math.max(...nums), log = hi / lo > 3, f = log ? Math.log : (v) => v;
    const W = 96, H = 30, P = 3, span = f(hi) - f(lo) || 1;
    const y = (v) => P + (1 - (f(v) - f(lo)) / span) * (H - 2 * P);
    const x = (i) => P + (i / (vals.length - 1)) * (W - 2 * P);
    const pts = vals.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");
    const t0 = +new Date(s.t0), t1 = +new Date(s.t1), cx = P + Math.max(0, Math.min(1, (+new Date(t.call_at) - t0) / (t1 - t0))) * (W - 2 * P);
    const v = sinceCall(t);
    return `<svg class="mc-spark ${pctCls(v ?? 0)}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
      <line class="lvl" x1="${P}" x2="${W - P}" y1="${y(t.call_price).toFixed(1)}" y2="${y(t.call_price).toFixed(1)}"/>
      <polyline points="${pts}"/><circle cx="${cx.toFixed(1)}" cy="${y(t.call_price).toFixed(1)}" r="2.6"/></svg>`;
  }

  // Columns: [key, label, tooltip, more-only, hidden on phones, sort value, cell].
  const COLS = [
    ["symbol", "Token", "Symbol, chain and name. Tokens sharing a name are different contracts, grouped under the latest.", false, false,
      (t) => name(t).toLowerCase(), tokenCell],
    ["price_usd", "Price", "Median of the fresh sources that agree; dimmed = stale (unknown now)", false, false,
      (t) => t.price_usd ?? t.last_price_usd, priceCell],
    ["call_at", "Called at", "The price when the first group call was posted: the pool's last trade before it (else our own price just after). Sorts by when.", false, true,
      (t) => (t.call_price ? ms(t.call_at) : null), calledCell],
    ["since", "Since call", "Price now against the price at the first call; peak = the highest trade since, as a multiple of the call price", false, false,
      sinceCall, sinceCell],
    ["chart", "Chart", "Price since just before the first call. Dashed line = the call price, dot = the call. Sorts like Since call.", false, true,
      sinceCall, (t) => `<td class="mc-spark-td">${spark(t)}</td>`],
    ["liquidity_usd", "Liquidity", "Money in the pricing pool; – = not reported (never read as $0)", false, true,
      (t) => t.liquidity_usd, (t) => `<td title="${t.liquidity_src ? `${esc(SRC[t.liquidity_src] || t.liquidity_src)}, ${ago(t.liquidity_ts)}` : "Not reported"}">${usd(t.liquidity_usd)}</td>`],
    ["mcap_usd", "Market cap", "From DexScreener; often not reported on pump.fun bonding curves", false, true,
      (t) => t.mcap_usd, (t) => `<td>${usd(t.mcap_usd)}</td>`],
    ["n_calls_24h", "Calls 24h", "Group calls in the last 24 hours", false, true,
      (t) => t.n_calls_24h, (t) => `<td title="${plural(t.n_calls ?? 0, "call")} in all, from ${plural(t.n_groups ?? 0, "group")}">${t.n_calls_24h || "–"}</td>`],
    ["activity", "Last activity", "The latest group call or bot buy", false, true,
      lastActivity, (t) => `<td class="cat" title="Last call ${esc(ago(t.last_call))}${t.last_entry ? ` · last bot buy ${esc(ago(t.last_entry))}` : ""}">${lastActivity(t) ? ago(lastActivity(t)) : "–"}</td>`],
    ["n_open", "Bot", "Real-money bot positions: open now, else closed", false, true,
      (t) => t.n_open * 1000 + t.n_trades, botCell],
    ["age_s", "Age", "Since its first pool (≥ = the pool's age is unknown, so since the first call)", false, true,
      (t) => t.age_s, (t) => `<td>${t.pool_age_s == null ? "≥" : ""}${dur(t.age_s)}</td>`],
    ["n_sources", "Price check", "Fresh sources agreeing within 5%", true, true,
      (t) => (t.stale ? -1 : t.n_sources), (t) => `<td class="left" data-tipid="${tipId(`<b>${esc(name(t))}</b><div class="tip-text">${esc(verdict(t))}</div>`)}">${checkBadge(t)}</td>`],
    ["price_age_s", "Price age", "How old the price is, against its limit for a token this age", true, true,
      (t) => t.price_age_s, (t) => `<td title="Limit ${dur(t.budget_s)} for a token this age">${dur(t.price_age_s)}</td>`],
    ["volume_h24_usd", "Volume 24h", "Traded in the pricing pool over 24 hours (DexScreener)", true, true,
      (t) => t.volume_h24_usd, (t) => `<td>${usd(t.volume_h24_usd)}</td>`],
    ["n_calls", "All calls", "Group calls since the token was first seen, and from how many groups", true, true,
      (t) => t.n_calls, (t) => `<td>${t.n_calls ?? 0}<span class="cat"> · ${t.n_groups ?? 0}g</span></td>`],
  ];

  function tokenCell(t, twins) {
    // The address tells same-name contracts apart (so it leads); a token without a symbol already shows it as its
    // name. Phones drop the Last activity column and show it here instead.
    const addr = t.symbol && (twins || !t.name) ? `<span class="mono">${esc(short(t.address))}</span>` : "";
    const nm = t.name && t.name !== t.symbol ? esc(t.name) : "";
    const sub = (twins ? [addr, nm] : [nm, addr]).filter(Boolean).join(" · ");
    const when = lastActivity(t) ? `<span class="mc-when">${ago(lastActivity(t))}${sub ? " · " : ""}</span>` : "";
    const open = S.expanded.has(symKey(t));
    const more = twins && !t.twin ? `<button class="mc-twins" data-sym="${esc(symKey(t))}" aria-expanded="${open}"
      title="Different contracts using the same name: often copycats">${open ? "hide the others" : `+${twins}<span class="mc-long"> more named ${esc(t.symbol)}</span><span class="mc-short"> same name</span>`}</button>` : "";
    return `<td class="left mc-tok"><span class="sym">${esc(name(t))}</span> <span class="mc-chain" title="${esc(CHAIN_NAME[t.chain] || t.chain)}">${esc(CHAIN[t.chain] || t.chain)}</span>${more}
      ${sub || when ? `<span class="sub${sub ? "" : " mc-when-only"}">${when}${sub}</span>` : ""}</td>`;
  }

  function priceCell(t) {
    if (t.price_usd != null) return `<td title="${esc(verdict(t))}">${price(t.price_usd)}${flagged(t, "disagree") ? ` <span class="mc-warn" aria-label="sources disagree">⚠</span>` : ""}</td>`;
    if (t.last_price_usd != null) return `<td title="${esc(verdict(t))}"><span class="mc-dim">${price(t.last_price_usd)}</span> <span class="mc-stale">stale</span></td>`;
    return `<td class="cat" title="${esc(verdict(t))}">${t.pool ? "no quote" : "no pool"}</td>`;
  }

  function botCell(t) {
    if (!t.n_trades) return `<td class="cat">–</td>`;
    return `<td title="${plural(t.n_trades, "real-money position")}, ${t.n_open} open">${t.n_open ? `<span class="mc-held">${t.n_open} open</span>` : `<span class="cat">${t.n_trades} closed</span>`}</td>`;
  }

  // Same-name tokens (different contracts) sit together under the first of them in the current order.
  function grouped(rows) {
    const groups = new Map();
    for (const t of rows) {
      const k = symKey(t) || `@${t.address}`;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(t);
    }
    return [...groups.values()];
  }

  function renderKpis(all, now) {
    const h = S.health;
    const k = (label, v, d, tone) => `<div class="kpi" style="--tone:${tone}"><div class="k">${label}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
    if (S.view === "retired") {
      $("#mc-kpis").innerHTML = k("Retired tokens", all.length.toLocaleString(), "no longer polled: 14 days old, or liquidity under $500 for a day", "var(--axis)");
      return;
    }
    const called = all.filter((t) => t.n_calls_24h > 0).length, fresh = all.filter((t) => t.price_usd != null).length;
    const two = all.filter((t) => t.price_usd != null && t.n_sources >= 2).length;
    const held = all.filter((t) => t.n_open > 0).length, bought = all.filter((t) => now - (ms(t.last_entry) || 0) < DAY).length;
    const rec = (h?.reconciliation || []).filter((r) => r.reference === "geckoterminal");
    const n = rec.reduce((a, r) => a + r.n, 0);
    const gap = n ? rec.reduce((a, r) => a + r.median_abs_err_pct * r.n, 0) / n : null;
    const inside = rec.reduce((a, r) => a + (r.in_range ?? 0) * r.n_in_range, 0) / (rec.reduce((a, r) => a + r.n_in_range, 0) || 1);
    $("#mc-kpis").innerHTML = [
      k("Tokens watched", all.length.toLocaleString(), `${called.toLocaleString()} called in the last 24 h`, "var(--cream)"),
      k("Live price", fresh.toLocaleString(), `${share(fresh / (all.length || 1))} of them · ${two.toLocaleString()} confirmed by 2+ sources`, "var(--good)"),
      k("Bot holds", held.toLocaleString(), `${plural(bought, "token")} bought in the last 24 h`, "var(--bull)"),
      k("Price cross-check", gap == null ? "–" : pctAbs(gap), gap == null ? "no check in the last 24 h yet"
        : `typical gap between sources · ${share(inside)} inside the minute's range`, gap == null ? "var(--axis)" : gap < 2 && inside >= 0.85 ? "var(--good)" : "var(--warning)"),
    ].join("");
  }

  function render() {
    if (!S.data) {
      // The shared failure states (app.js "load states"), retried here every 15 s; "Try now" runs load().
      if (S.error) status(`<div class="load-state failed"><b>Couldn't load the micro-caps.</b> <span class="msg">${esc(cap(S.error))}. Retrying every 15 seconds.</span>
        <button class="load-retry" data-retry="micro">Try now</button></div>`);
      return;
    }
    const d = S.data, now = Date.now(), all = d.tokens;
    status(S.error ? `<div class="load-note"><span><b>Couldn't refresh the micro-caps:</b> ${esc(S.error)}. Showing the list from ${esc(ago(d.now))}; retrying every 15 seconds.</span>
        <button class="load-retry" data-retry="micro">Try now</button></div>`
      : d.ready ? "" : `<div class="mc-error">The price pipeline isn't deployed yet (migration 008): calls and trades only.</div>`);
    renderKpis(all, now);

    // chips and the chain menu carry their counts
    for (const b of tab.querySelectorAll("#mc-view button")) {
      const v = b.dataset.view;
      b.querySelector(".mc-n").textContent = v === "retired" || S.view === "retired" ? "" : ` ${all.filter((t) => inView[v](t, now)).length}`;
    }
    const chains = Object.entries(all.reduce((a, t) => ({ ...a, [t.chain]: (a[t.chain] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1]);
    const opts = `<option value="">All chains</option>` + chains.map(([c, n]) =>
      `<option value="${esc(c)}"${c === S.chain ? " selected" : ""}>${esc(CHAIN_NAME[c] || c)} (${n})</option>`).join("");
    if (opts !== S.opts) { $("#mc-chain").innerHTML = opts; S.opts = opts; }   // untouched while unchanged, so an open menu stays open

    const cols = COLS.filter((c) => S.more || !c[3]);
    const col = COLS.find((c) => c[0] === S.sort) || COLS.find((c) => c[0] === "activity");
    const val = (t) => col[5](t) ?? null;
    // Missing values (no liquidity reported, never called...) sort last in either direction.
    const rows = visible(all, now).sort((a, b) => {
      const x = val(a), y = val(b);
      return x == null || y == null ? (x == null) - (y == null) : (x > y ? 1 : x < y ? -1 : 0) * S.dir;
    });
    const groups = grouped(rows);
    const head = `<thead><tr>${cols.map(([k, label, title, , phone]) => `<th data-k="${k}" class="${k === "symbol" || k === "n_sources" ? "left" : ""}${phone ? " mc-opt" : ""}${S.sort === k ? " sorted" : ""}" title="${esc(title)}"${S.sort === k ? ` aria-sort="${S.dir > 0 ? "ascending" : "descending"}"` : ""}>${label}${S.sort === k ? (S.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead>`;
    tips.clear();
    const line = (t, twins) => `<tr data-key="${esc(key(t))}" tabindex="0" class="drill${t.twin ? " mc-twin" : ""}">${cols.map((c) => {
      const html = c[0] === "symbol" ? tokenCell(t, twins) : c[6](t);
      return c[4] ? html.replace(/^<td( class="([^"]*)")?/, (m, _, cls) => `<td class="${cls ? `${cls} ` : ""}mc-opt"`) : html;
    }).join("")}</tr>`;
    const body = groups.slice(0, S.shown).map((g) => {
      const [lead, ...rest] = g;
      return line(lead, rest.length) + (S.expanded.has(symKey(lead)) ? rest.map((t) => line({ ...t, twin: true }, rest.length)).join("") : "");
    }).join("");
    const empty = `<tr><td colspan="${cols.length}" class="left mc-empty">${S.view === "retired" && !all.length
      ? "Nothing retired yet: a token retires 14 days after it was first seen, or once its liquidity stays under $500 for a day."
      : `No tokens match${S.q ? ` “${esc(S.q)}”` : ""} in this view.`}
      ${S.view !== "all" || S.chain || S.q ? `<button class="more-toggle mc-reset">Show every token</button>` : ""}</td></tr>`;
    $("#mc-table").innerHTML = head + `<tbody>${body || empty}</tbody>`;
    $("#mc-foot").innerHTML = rows.length ? `<span class="cat">${groups.length > S.shown ? `Showing the first ${S.shown} of ${groups.length} names` : `${plural(groups.length, "name")}`}${rows.length !== groups.length ? ` (${plural(rows.length, "token")}: same-name contracts are grouped)` : ""}. Click a token for its dossier: the evidence, the chart and the calls.</span>
      ${groups.length > S.shown ? `<button class="more-toggle mc-page">Show ${Math.min(PAGE, groups.length - S.shown)} more</button>` : ""}` : "";
  }

  // ─── price accuracy (Micro-caps fold and Health tab) ────────────────────
  function renderAccuracy(host) {
    const h = S.health;
    if (!host || !h) return;
    const rec = h.reconciliation || [];
    const sum = $("#mc-acc-sum");
    if (sum && host.closest("#tab-micro")) {
      const g = rec.filter((r) => r.reference === "geckoterminal"), n = g.reduce((a, r) => a + r.n, 0);
      sum.textContent = n ? `· typical gap ${pctAbs(g.reduce((a, r) => a + r.median_abs_err_pct * r.n, 0) / n)} over ${n.toLocaleString()} quotes, last 24 h` : "· no check in the last 24 h";
    }
    const pairs = rec.map((r) => `<tr><td class="left">${esc(SRC[r.source])} vs ${esc(r.reference === "geckoterminal" ? "GeckoTerminal 1m candle" : SRC[r.reference])}</td>
      <td>${r.n.toLocaleString()}</td><td>${r.tokens}</td><td>${pctAbs(r.median_abs_err_pct)}</td><td>${pctAbs(r.p90_abs_err_pct, 1)}</td>
      <td>${share(r.within_1pct)}</td><td>${share(r.within_5pct)}</td><td>${r.n_in_range ? share(r.in_range) : "–"}</td></tr>`).join("");
    const cov = (h.coverage || []).map((c) => `<tr><td class="left">${esc(CHAIN_NAME[c.chain] || c.chain)}</td><td>${c.tokens}</td><td>${c.with_pool}</td><td>${c.fresh}</td><td>${c.stale}</td>
      <td>${c.no_price ?? "–"}</td><td>${c.single_source}</td><td>${c.disagree}</td><td>${c.rejected_print}</td><td>${c.with_candles}</td></tr>`).join("");
    host.innerHTML = `<div class="table-wrap"><table class="plain"><thead><tr><th class="left">Comparison</th><th>Quotes</th><th>Tokens</th>
      <th title="Median absolute difference">Median gap</th><th title="90% of quotes are closer than this">p90 gap</th><th>Within 1%</th><th>Within 5%</th><th title="Quote inside that minute's low…high (±1%)">Inside the candle</th></tr></thead>
      <tbody>${pairs || `<tr><td colspan="8" class="left">No check in the last 24 h (hourly, 20 random tokens).</td></tr>`}</tbody></table></div>
      <p class="note">Each hour 20 random tokens are re-checked: every Jupiter and DexScreener quote against the GeckoTerminal 1-minute candle of the same minute, and Jupiter against DexScreener.
      ${h.last_run ? `Last run ${ago(h.last_run)}.` : ""} Re-deriving candles from on-chain swaps comes with the M2 swap feed.</p>
      <h3>Coverage by chain <span class="cat">${h.retired ?? 0} retired</span></h3>
      <div class="table-wrap"><table class="plain"><thead><tr><th class="left">Chain</th><th>Tokens</th><th title="Has a pool where it is the token being traded">With a pool</th><th>Fresh price</th><th title="Had a price, now older than its limit">Stale</th>
      <th title="No pool or no quote yet">No price yet</th><th>One source</th><th>Disagree</th><th title="A >10× jump held back until confirmed">Held back</th><th>With candles</th></tr></thead><tbody>${cov || `<tr><td colspan="10" class="left">No tokens yet.</td></tr>`}</tbody></table></div>`;
  }

  async function healthBlock() {
    let host = $("#mc-health");
    if (!host) {
      $("#tab-health").insertAdjacentHTML("beforeend", `<h3>Micro-cap price accuracy</h3><div id="mc-health"></div>`);
      host = $("#mc-health");
    }
    S.health = await get("/api/micro/health");
    renderAccuracy(host);
  }

  // ─── token drawer ────────────────────────────────────────────────────────
  function showDrawer(html) {
    $("#drawer-body").innerHTML = html;
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }

  const RANGES = [["call", "Since call"], ["60", "1h"], ["360", "6h"], ["1440", "24h"], ["10080", "7d"]];
  function rangeMinutes(r, callAt) {
    if (r !== "call") return Number(r);
    if (!callAt) return 720;
    const since = (Date.now() - ms(callAt)) / 60_000;
    return Math.round(Math.min(20160, Math.max(30, since + Math.max(15, since * 0.1))));
  }
  const tokenUrl = (k, minutes) => {
    const [chain, address] = k.split("/");
    return `/api/micro/token/${encodeURIComponent(chain)}/${encodeURIComponent(address)}?minutes=${minutes}`;
  };

  async function openToken(k) {
    const [, address] = k.split("/");
    S.open = k;
    const listed = (S.data?.tokens || []).find((x) => key(x) === k);
    S.range = listed && !listed.call_at ? "1440" : "call";
    let d;
    try {
      d = await get(tokenUrl(k, rangeMinutes(S.range, listed?.call_at)));
      if (!listed && d.token.call_at && rangeMinutes("call", d.token.call_at) > 720) d = await get(tokenUrl(k, rangeMinutes("call", d.token.call_at)));
      if (!d.token.call_at && S.range === "call") S.range = "1440";
    } catch (e) {
      showDrawer(`<h2>${esc(short(address || k))}</h2><p class="mc-error">${/404$/.test(e.message)
        ? "This token isn't on the watchlist (or the link is wrong)." : `Couldn't load this token (${esc(errText(e))}).`}</p>`);
      return;
    }
    if (S.open !== k) return;            // another row was clicked meanwhile
    const t = S.openTok = d.token;
    const net = EXPLORER[t.chain];
    const links = [t.pool && net ? `<a href="https://dexscreener.com/${net}/${esc(t.pool)}" target="_blank" rel="noopener">DexScreener ↗</a>` : "",
      t.pool && net ? `<a href="https://www.geckoterminal.com/${net === "ethereum" ? "eth" : net}/pools/${esc(t.pool)}" target="_blank" rel="noopener">GeckoTerminal ↗</a>` : ""].filter(Boolean).join(" · ");
    const real = d.trades.filter((x) => !x.dry), open = real.filter((x) => x.status === "open").length;
    const pnl = real.reduce((a, x) => a + (x.real_pnl_usd ?? 0), 0), hasPnl = real.some((x) => x.real_pnl_usd != null);
    const since = sinceCall(t);
    const kv = [
      ["Price", `${price(t.price_usd ?? t.last_price_usd)}${t.stale && t.last_price_usd != null ? ` <span class="mc-stale">stale</span>` : ""}`,
        t.price_ts ? `as of ${ago(t.price_ts)}` : "no quote yet"],
      ["Called at", t.call_price ? price(t.call_price) : "–", t.call_price ? `${ago(t.call_at)}${t.call_group ? ` · ${t.call_group}` : ""}`
        : t.call_at ? "no trade near the call" : "never called"],
      ["Since call", since == null ? "–" : `<span class="mc-since ${pctCls(since)}">${fmtSince(since)}</span>`,
        t.peak_x ? `peak ${xs(t.peak_x)}${t.peak_at ? `, ${ago(t.peak_at)}` : ""}` : ""],
      ["Liquidity", usd(t.liquidity_usd), t.liquidity_src ? `${SRC[t.liquidity_src]}, ${ago(t.liquidity_ts)}` : "not reported"],
      ["Market cap", usd(t.mcap_usd), t.fdv_usd != null && t.fdv_usd !== t.mcap_usd ? `fully diluted ${usd(t.fdv_usd)}` : t.mcap_usd == null ? "not reported" : ""],
      ["Age", `${t.pool_age_s == null ? "≥ " : ""}${dur(t.age_s)}`, t.pool_age_s == null ? "since the first call" : "since its first pool"],
      ["Calls", (t.n_calls ?? 0).toLocaleString(), `${t.n_calls_24h || 0} in 24 h · ${plural(t.n_groups ?? 0, "group")}`],
      ["Bot", real.length ? `${open} open` : "–", real.length ? `${plural(real.length, "position")}${hasPnl ? ` · ${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl).toFixed(2)} realised` : ""}` : "never traded"],
    ];
    const cons = t.price_usd ?? t.last_price_usd;
    const latest = {};
    for (const q of d.quotes) if (q.source !== "consensus") latest[q.source] = q;
    const srcRows = Object.values(latest).map((q) => `<tr><td class="left">${esc(SRC[q.source] || q.source)}</td><td>${price(q.price_usd)}</td>
      <td>${cons && q.price_usd ? fmtPct((q.price_usd / cons - 1) * 100, 2) : "–"}</td><td>${ago(q.source_ts)}</td><td class="left cat">${esc((q.flags || []).join(", "))}</td></tr>`).join("");
    const pools = d.pools.map((p) => `<tr><td class="left">${p.pool === t.pool ? "● " : ""}${esc(p.dex || "?")} ${esc((p.labels || []).join(" "))} <span class="sub mono">${esc(short(p.pool))}</span></td>
      <td class="left">${p.is_base ? `traded against ${esc(p.quote_symbol || "?")}` : `<span title="Here the token is the currency, not the thing traded: never used for its price">✕ used as the currency</span>`}</td>
      <td>${usd(p.liquidity_usd)}</td><td class="cat">${p.created_at ? ago(p.created_at) : "–"}</td><td class="left cat">${p.migrated_from ? `migrated from ${esc(short(p.migrated_from))}` : ""}</td></tr>`).join("");
    const switches = d.switches.map((s) => `<li>${utc(s.ts)}: ${s.from_pool ? `${esc(short(s.from_pool))} (${usd(s.from_liquidity)}) → ` : ""}${esc(short(s.to_pool))} (${usd(s.to_liquidity)}), ${esc(s.reason)}</li>`).join("");
    const calls = [...d.calls].reverse(), now = livePx(t);
    const callRows = calls.slice(0, 50).map((c) => {
      const ch = c.price && now ? (now / c.price - 1) * 100 : null;
      return `<tr><td class="left cat">${ago(c.called_at)}</td><td class="left">${esc(c.group_name || "–")}</td><td class="left">${esc(c.caller || "–")}</td>
        <td class="left">${c.intent === "buy" ? "buy call" : c.intent === "sell" ? "sell call" : "mention"}</td>
        <td title="${c.price ? `Priced from ${esc(howPriced(c.price_src, c.price_after_min))}` : "No trade near this call to price it from"}">${c.price ? price(c.price) : "–"}</td>
        <td>${ch == null ? "–" : `<span class="mc-since ${pctCls(ch)}">${fmtSince(ch)}</span>`}</td></tr>`;
    }).join("");
    // data-label: on phones each trade becomes a small labelled card instead of a 7-column row (gallery.css)
    const trades = [...d.trades].reverse().map((x) => `<tr><td class="left cat" data-label="Entry">${x.entry_at ? ago(x.entry_at) : "–"}</td><td class="left" data-label="Strategy">${esc(x.strategy || "")}${x.dry ? " <span class=\"cat\">(paper)</span>" : ""}</td>
      <td data-label="Size">${usd(x.size_usd)}</td><td data-label="Entry price">${price(x.entry_price)}</td><td class="left" data-label="Status">${esc(x.status || "")}</td>
      <td data-label="Realised">${x.realized_x == null ? "–" : `${x.realized_x.toFixed(2)}×`}</td><td data-label="P&amp;L">${x.real_pnl_usd == null ? "–" : `${x.real_pnl_usd >= 0 ? "+" : "−"}$${Math.abs(x.real_pnl_usd).toFixed(2)}`}</td></tr>`).join("");
    const twins = (S.data?.tokens || []).filter((x) => symKey(x) && symKey(x) === symKey(t) && x.address !== t.address)
      .sort((a, b) => (b.n_calls ?? 0) - (a.n_calls ?? 0));
    // The Deep End's dossier (deep.js) has the evidence; this drawer is the price check. On that dossier already, the
    // button just closes the drawer.
    const onIt = state.tab === "dossier" && state.deep?.chain === t.chain && state.deep?.address === t.address;
    showDrawer(`
      <h2>${esc(name(t))} <span class="cat mc-name">${t.name && t.name.toLowerCase() !== (t.symbol || "").toLowerCase() ? esc(t.name) : ""}</span></h2>
      ${window.deepOpen ? `<a class="btn-paint mc-dossier" href="#/deep/${esc(t.chain)}/${esc(t.address)}" data-dossier="${esc(t.chain)}/${esc(t.address)}" data-label="${esc(name(t))}">${onIt ? "Back to the dossier" : "Open the full dossier"}</a>` : ""}
      <div class="mc-idline"><span class="mc-chain">${esc(CHAIN[t.chain] || t.chain)}</span> <span class="cat">${esc(CHAIN_NAME[t.chain] || t.chain)}${t.launchpad ? ` · launched on ${esc(t.launchpad)}` : ""}</span>
        <span class="mono mc-addr">${esc(t.address)}</span> <button class="more-toggle mc-copy" data-copy="${esc(t.address)}">Copy address</button>${links ? ` <span class="mc-links">${links}</span>` : ""}</div>
      ${t.call_price ? `<p class="mc-callline">First called <b>${ago(t.call_at)}</b>${t.call_group ? ` in ${esc(t.call_group)}` : ""} at <b>${price(t.call_price)}</b>;
        now ${price(now)}${since == null ? "" : `, <span class="mc-since ${pctCls(since)}">${fmtSince(since)}</span>`}${t.peak_x ? `. Highest since: ${xs(t.peak_x)} the call price${t.peak_at ? ` (${esc(utc(t.peak_at))})` : ""}` : ""}.</p>` : ""}
      <div class="mc-chart-h first"><h3>Price <span class="cat" id="mc-chart-sub"></span></h3>
        <div class="chips mc-range" role="group" aria-label="Chart range">${RANGES.filter(([r]) => r !== "call" || t.call_at)
          .map(([r, label]) => `<button data-range="${r}" class="${r === S.range ? "on" : ""}">${label}</button>`).join("")}</div></div>
      <div id="mc-chart"></div>
      <p class="mc-verdict">${esc(verdict(t))}</p>
      <div class="mc-kv">${kv.map(([k, v, s]) => `<div><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${esc(s)}</div>` : ""}</div>`).join("")}</div>

      <h3>Calls <span class="cat">${plural(calls.length, "call")}, newest first</span></h3>
      ${calls.length ? `<div class="table-wrap"><table class="plain mc-calls"><thead><tr><th class="left">When</th><th class="left">Group</th><th class="left">Caller</th><th class="left">Kind</th>
        <th title="The price when this call was posted">Called at</th><th title="Price now against the price at this call">Since</th></tr></thead>
        <tbody>${callRows}</tbody></table></div>${calls.length > 50 ? `<p class="note">The 50 newest of ${calls.length}.</p>` : ""}` : `<p class="note">No group has called it: it came in through a bot trade.</p>`}
      <h3>Bot trades <span class="cat">${d.trades.length ? plural(d.trades.length, "position") : ""}</span></h3>
      ${trades ? `<div class="table-wrap"><table class="plain mc-trades"><thead><tr><th class="left">Entry</th><th class="left">Strategy</th><th>Size</th><th>Entry price</th><th class="left">Status</th><th>Realised</th><th>P&amp;L</th></tr></thead>
        <tbody>${trades}</tbody></table></div>` : `<p class="note">The bot hasn't traded it.</p>`}
      ${twins.length ? `<h3>Other tokens named ${esc(t.symbol)} <span class="cat">different contracts, often copycats</span></h3>
        <ul class="mc-twin-list">${twins.map((x) => `<li><button data-key="${esc(key(x))}"><span class="mc-chain">${esc(CHAIN[x.chain] || x.chain)}</span> <span class="mono">${esc(short(x.address))}</span>
          <span class="cat">${price(x.price_usd ?? x.last_price_usd)} · liquidity ${usd(x.liquidity_usd)} · ${plural(x.n_calls ?? 0, "call")}</span></button></li>`).join("")}</ul>` : ""}
      <details class="fold"><summary>How this price is made</summary>
        <p class="note">Polled ${esc(TIER[t.tier] || t.tier)}. A token this age needs a price under ${dur(t.budget_s)} old.</p>
        <h3>Latest stored quote per source</h3><div class="table-wrap"><table class="plain"><thead><tr><th class="left">Source</th><th>Price</th><th>vs the price now</th><th>Quoted</th><th class="left">Flags</th></tr></thead>
          <tbody>${srcRows || `<tr><td colspan="5" class="left">No quotes in the last 6 hours.</td></tr>`}</tbody></table></div>
        <h3>Pools <span class="cat">● = the one it is priced from: the deepest where it is the token being traded</span></h3>
        <div class="table-wrap"><table class="plain"><thead><tr><th class="left">Pool</th><th class="left">Role</th><th>Liquidity</th><th>Created</th><th></th></tr></thead>
          <tbody>${pools || `<tr><td colspan="5" class="left">No pool found yet.</td></tr>`}</tbody></table></div>
        ${switches ? `<h3>Pricing pool changes</h3><ul class="plain-list">${switches}</ul>` : ""}
      </details>`);
    drawChart(t, d);
  }

  function drawChart(t, d) {
    const host = $("#drawer-body #mc-chart");
    if (!host) return;
    const r = RANGES.find(([k]) => k === S.range);
    $("#drawer-body #mc-chart-sub").textContent = r ? (S.range === "call" ? "since just before the first call" : `last ${r[1]}`) : "last 24h";
    candleChart(host, d.candles, d.calls, d.trades, t.call_price, d.bucket_min || 1, { rejected: d.rejected || 0, until: d.candles_until, since: d.since });
  }

  async function setRange(r) {
    const k = S.open;
    if (!k) return;
    S.range = r;
    $("#drawer-body").querySelectorAll(".mc-range button").forEach((b) => b.classList.toggle("on", b.dataset.range === r));
    const d = await get(tokenUrl(k, rangeMinutes(r, S.openTok?.call_at)));
    if (S.open === k && S.range === r) drawChart(d.token, d);
  }

  // Hollow candles = up, filled = down, so direction never rests on colour. Log scale when the range is wide.
  // candles: the pool's 1-minute candles up to `until` (their last GeckoTerminal fetch), then bars built from our
  // own live quotes (quote=true: drawn thinner, no volume). Held-back prints are already left out (`rejected`).
  function candleChart(host, candles, calls, trades, callPrice, bucketMin = 1, { rejected = 0, until = null, since = null } = {}) {
    const good = candles;
    if (good.length < 2) { host.innerHTML = `<p class="note">${good.length ? "One traded minute so far." : "No candles yet: GeckoTerminal is polled by age (new tokens every 5 min)."}</p>`; return; }
    const W = host.clientWidth || 660, H = 260, L = 8, R = 72, T = 14, B = 40, VH = 36;
    // The window runs to now, so a call made since the last price change still gets its marker.
    // ...and starts where the window asked for (just before the first call), even if data begins later.
    const t0 = Math.min(+new Date(good[0].ts), since ? +new Date(since) : Infinity), t1 = Math.max(+new Date(good[good.length - 1].ts) + 60_000, Date.now());
    const maxBars = Math.floor((W - L - R) / 3);
    const step = Math.max(1, Math.ceil((t1 - t0) / 60_000 / maxBars)) * 60_000;
    const bars = new Map();
    for (const c of good) {
      const b = Math.floor((+new Date(c.ts) - t0) / step);
      const x = bars.get(b);
      if (!x) bars.set(b, { t: t0 + b * step, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume_usd || 0, n: 1, single: !!c.single, quote: !!c.quote });
      else Object.assign(x, { h: Math.max(x.h, c.high), l: Math.min(x.l, c.low), c: c.close, v: x.v + (c.volume_usd || 0), n: x.n + 1, quote: x.quote && !!c.quote });
    }
    const bs = [...bars.values()];
    let lo = Math.min(...bs.map((b) => b.l)), hi = Math.max(...bs.map((b) => b.h));
    // The first call's price joins the scale when it is near the window's prices, so its line is on the chart.
    const level = callPrice && callPrice >= lo / 3 && callPrice <= hi * 3 ? callPrice : null;
    if (level) { lo = Math.min(lo, level); hi = Math.max(hi, level); }
    const log = hi / lo > 5;
    const f = log ? Math.log10 : (v) => v;
    const pad = (f(hi) - f(lo)) * 0.05 || Math.abs(f(hi)) * 0.01;
    const ylo = f(lo) - pad, yhi = f(hi) + pad;
    const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
    const y = (v) => T + (1 - (f(v) - ylo) / (yhi - ylo)) * (H - T - B - VH);
    const vmax = Math.max(...bs.map((b) => b.v)) || 1;
    const bw = Math.max(1, ((W - L - R) / ((t1 - t0) / step)) * 0.7);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((k) => (log ? 10 ** (ylo + k * (yhi - ylo)) : ylo + k * (yhi - ylo)));
    const candlesSvg = bs.map((b) => {
      const cx = x(b.t + step / 2), up = b.c >= b.o, top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
      const q = b.quote ? " q" : "";
      return `<line class="wick ${up ? "up" : "down"}${q}" x1="${cx}" x2="${cx}" y1="${y(b.h)}" y2="${y(b.l)}"/>` +
        `<rect class="body ${up ? "up" : "down"}${q}" x="${cx - bw / 2}" y="${top}" width="${bw}" height="${Math.max(1, bot - top)}"/>` +
        `<rect class="vol" x="${cx - bw / 2}" y="${H - B - (b.v / vmax) * VH}" width="${bw}" height="${(b.v / vmax) * VH}"/>`;
    }).join("");
    const inRange = (ts) => +new Date(ts) >= t0 && +new Date(ts) <= t1;
    const callTitle = (c) => `<title>Call ${utc(c.called_at)} ${esc(c.group_name || "")}${c.price ? ` at ${price(c.price)}` : ""}</title>`;
    const callMarks = calls.filter((c) => inRange(c.called_at)).map((c) => `<path class="call" d="M${x(+new Date(c.called_at))},${T - 2} l-4,-7 h8 z">${callTitle(c)}</path>` +
      (c.price && c.price >= lo && c.price <= hi ? `<circle class="call-at" cx="${x(+new Date(c.called_at))}" cy="${y(c.price)}" r="4.5">${callTitle(c)}</circle>` : "")).join("");
    const levelLine = level ? `<line class="call-lvl" x1="${L}" x2="${W - R}" y1="${y(level)}" y2="${y(level)}"/>
      <rect class="call-lbl-bg" x="${L + 1}" y="${y(level) - 18}" width="${16 + 6.6 * (7 + price(level).length)}" height="15" rx="4"/>
      <text class="axis-label call-lbl" x="${L + 6}" y="${y(level) - 6.5}">called ${price(level)}</text>` : "";
    // Where the pool's candles stop and our live quotes take over, when that is inside the window.
    const cut = until ? +new Date(until) : null, gap = cut ? (Date.now() - cut) / 60_000 : 0;
    const cutLine = cut && cut > t0 && cut < t1 && gap > 5 ? `<line class="cut" x1="${x(cut)}" x2="${x(cut)}" y1="${T}" y2="${H - B}"/>
      <text class="axis-label cut-lbl" x="${x(cut) + 4}" y="${H - B - 4}">live quotes →</text>` : "";
    const entryMarks = trades.filter((e) => e.entry_price && inRange(e.entry_at) && e.entry_price >= lo / 2 && e.entry_price <= hi * 2)
      .map((e) => `<circle class="entry" cx="${x(+new Date(e.entry_at))}" cy="${y(e.entry_price)}" r="4"><title>Bot entry ${utc(e.entry_at)} at ${e.entry_price}</title></circle>`).join("");
    host.innerHTML = `<svg class="chart mc-candles" viewBox="0 0 ${W} ${H}" role="img" aria-label="1 minute candles${log ? ", log scale" : ""}">
      ${ticks.map((v) => `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-label" x="${W - R + 6}" y="${y(v) + 4}">${price(v)}</text>`).join("")}
      <text class="axis-label" x="${L}" y="${H - 6}">${utc(t0).slice(5)}</text><text class="axis-label" x="${W - R}" y="${H - 6}" text-anchor="end">${utc(t1).slice(5)}</text>
      ${cutLine}${candlesSvg}${levelLine}${callMarks}${entryMarks}
      <line class="cross" id="mc-cx" y1="${T}" y2="${H - B}" visibility="hidden"/>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="mc-hit"/></svg>
      <p class="note">${bs.length} bars of ${Math.max(step / 60_000, bucketMin)} min${log ? ", log scale" : ""}. Hollow = closed up, filled = closed down. ▼ = a call, ◯ = the price it was called at${level ? ", dashed line = the first call's price" : ""}, ● = a bot entry.${cutLine ? ` The pool's candles (GeckoTerminal, free tier) are ${Math.round(gap)} min behind: bars after ${esc(utc(cut).slice(11))} are built from our own live quotes (thinner, no volume).` : ""}${rejected ? ` ${rejected} held-back print${rejected > 1 ? "s" : ""} left out (a >10× move nobody confirmed).` : ""}</p>`;
    const svg = host.querySelector("svg"), cx = $("#mc-cx", svg);
    $("#mc-hit", svg).addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(), t = t0 + (((e.clientX - r.left) / r.width) * W - L) / (W - L - R) * (t1 - t0);
      const b = bars.get(Math.floor((t - t0) / step));
      if (!b) { hideTip(); return; }
      cx.setAttribute("x1", x(b.t + step / 2)); cx.setAttribute("x2", x(b.t + step / 2)); cx.setAttribute("visibility", "visible");
      showTip(e, `<b>${utc(b.t)}</b>${row("Open", price(b.o))}${row("High", price(b.h))}${row("Low", price(b.l))}` +
        `${row("Close", price(b.c))}${row("Change", fmtPct((b.c / b.o - 1) * 100, 1))}${row("Volume", usd(b.v))}${b.quote ? row("From", "our live quotes (candles not fetched yet)") : step === 60_000 ? row("Sources", b.single ? "GeckoTerminal only" : "confirmed by a quote") : row("Minutes traded", b.n)}`);
    });
    $("#mc-hit", svg).addEventListener("pointerleave", () => { cx.setAttribute("visibility", "hidden"); hideTip(); });
  }

  // ─── wiring ──────────────────────────────────────────────────────────────
  healthBlock = guarded("the micro-cap price check", ["#mc-health"], healthBlock);   // app.js "load states"
  retries.set("micro", () => load());      // the "Try now" button on the micro-caps failure states
  const refreshTab = () => load().catch((e) => console.error(e));
  const onClass = (el, fn) => new MutationObserver(() => el.classList.contains("on") && fn()).observe(el, { attributes: true, attributeFilter: ["class"] });
  onClass(tab, () => { S.health = null; refreshTab(); });
  onClass($("#tab-health"), () => healthBlock().catch((e) => console.error(e)));
  setInterval(() => { if (tab.classList.contains("on") && !document.hidden) refreshTab(); }, 15_000);
  setInterval(() => { S.health = null; }, 60_000);   // health (reconciliation, coverage) refetched once a minute

  const rerender = () => { S.shown = PAGE; render(); };
  $("#mc-view").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    tab.querySelectorAll("#mc-view button").forEach((x) => x.classList.toggle("on", x === b));
    const reload = (b.dataset.view === "retired") !== (S.view === "retired");
    S.view = b.dataset.view;
    if (reload) { S.data = null; S.shown = PAGE; $("#mc-table").innerHTML = ""; refreshTab(); } else rerender();
  });
  $("#mc-chain").addEventListener("change", (e) => { S.chain = e.target.value; rerender(); });
  $("#mc-search").addEventListener("input", (e) => { S.q = e.target.value; rerender(); });
  $("#mc-more").addEventListener("click", (e) => {
    S.more = !S.more;
    e.currentTarget.classList.toggle("on", S.more);
    e.currentTarget.setAttribute("aria-expanded", S.more);
    render();
  });
  $("#mc-foot").addEventListener("click", (e) => { if (e.target.closest(".mc-page")) { S.shown += PAGE; render(); } });
  // A token opens its Deep End dossier (deep.js); without it, the price drawer as before.
  const openRow = (k) => {
    const [chain, ...rest] = k.split("/"), tok = (S.data?.tokens || []).find((x) => key(x) === k);
    if (window.deepOpen) { window.deepOpen(chain, rest.join("/"), "micro", tok ? name(tok) : undefined); return; }
    history.replaceState(null, "", `#micro=${k}`);
    openToken(k).catch((err) => console.error(err));
  };
  // The dossier's "How this price is checked" opens this drawer over it, leaving the dossier's link alone.
  window.microOpen = (k) => { openToken(k).catch((err) => console.error(err)); };
  $("#mc-table").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-k]");
    if (th) { S.dir = S.sort === th.dataset.k ? -S.dir : th.dataset.k === "symbol" ? 1 : -1; S.sort = th.dataset.k; render(); return; }
    const twin = e.target.closest(".mc-twins");
    if (twin) { const k = twin.dataset.sym; S.expanded.has(k) ? S.expanded.delete(k) : S.expanded.add(k); render(); return; }
    if (e.target.closest(".mc-reset")) {
      Object.assign(S, { view: "all", chain: "", q: "" });
      $("#mc-search").value = "";
      tab.querySelectorAll("#mc-view button").forEach((x) => x.classList.toggle("on", x.dataset.view === "all"));
      rerender();
      return;
    }
    const tr = e.target.closest("tr[data-key]");
    if (tr) openRow(tr.dataset.key);
  });
  $("#mc-table").addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr[data-key]");
    if (tr && (e.key === "Enter" || e.key === " ") && e.target === tr) { e.preventDefault(); openRow(tr.dataset.key); }
  });
  $("#drawer-body").addEventListener("click", (e) => {
    const rb = e.target.closest(".mc-range button[data-range]");
    if (rb) { setRange(rb.dataset.range).catch((err) => console.error(err)); return; }
    const c = e.target.closest(".mc-copy");
    if (c) {
      navigator.clipboard?.writeText(c.dataset.copy).then(() => { c.textContent = "Copied"; setTimeout(() => { c.textContent = "Copy address"; }, 1500); }, () => {});
      return;
    }
    const b = e.target.closest(".mc-twin-list button[data-key]");
    if (b) openRow(b.dataset.key);
  });
  // Closing the drawer leaves the token's deep link behind, so a reload doesn't reopen it.
  $("#drawer-close").addEventListener("click", () => {
    S.open = null;
    if (location.hash.startsWith("#micro=")) history.replaceState(null, "", "#/micro");
  });

  // Deep links: #/micro, #micro=solana/<mint>
  if (tab.classList.contains("on")) refreshTab();
  if ($("#tab-health").classList.contains("on")) healthBlock().catch((e) => console.error(e));
  if (location.hash.startsWith("#micro=")) {
    setTab("micro");
    openToken(decodeURIComponent(location.hash.slice(7))).catch((e) => console.error(e));
  }
})();
