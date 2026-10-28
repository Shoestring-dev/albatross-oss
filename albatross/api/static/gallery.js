"use strict";
// Gallery shell: per-page mastheads, the Overview page, the sector treemap, the price ticker and the
// loader. Kept out of app.js; uses its helpers ($, esc, get, cellHtml, fmtPrice, fmtPct, fmtUsd, ago,
// humanize, setTab, state, TFS).
(() => {
  const ART = (name) => `url("/static/art/${name}.webp")`;
  const today = () => new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const MAST = {
    home: { kicker: () => `Is it working? · ${today()}`, title: "Is it <em>working</em>?", art: "hero-home" },
    // Results: every record, in dollars or against what it has to beat
    books: { kicker: "Results · Books", title: "What each rule <em>made</em>.", sub: "Every rule paper-traded from its first day, in dollars after fees, slippage and funding, beside what it has to beat and the date its verdict is due.", art: "hero-ideas" },
    book: { kicker: "Results · Book", title: "", art: "hero-ideas" },
    scorecard: { kicker: "Results · Sources", title: "Who has been <em>right</em>.", sub: "Are the ideas making money after costs, and which sources beat a one-way caller (always long or always short, whichever did better). The evidence folds open underneath.", art: "hero-matrix" },
    dreport: { kicker: "Results · Dossier report", title: "Does the dossier <em>pick</em> winners?", sub: "Two questions set in advance: do calls without a red flag do better, and do higher scores go with better returns. Each call is the $100 trade sold after 24 hours, all costs in.", art: "hero-deep", pos: "70% 12%" },
    ideas: { kicker: "Results · Ideas ledger", title: "Every idea, <em>on the record</em>.", sub: "Logged to an append-only, hash-chained ledger before the outcome exists. Open ideas first, grouped by coin; click one for its plan, the critic's view and how it's doing.", art: "hero-ideas" },
    reports: { kicker: "Results · Reports", title: "The desk <em>writes</em>.", sub: "The CEO memo twice a day, a brief every four hours, a daily wrap and the Oracle's thesis.", art: "hero-reports" },
    live: { kicker: "Alpha · Live", title: "Skip or pass, <em>live</em>.", sub: "Every memecoin call gets a verdict the moment it lands and a result a day later, so you can check whether the skips really died. This week's factor lists sit beside it. Paper only.", art: "hero-deep", pos: "70% 12%" },
    review: { kicker: "Alpha · Review", title: "What earns its <em>keep</em>.", sub: "Every part of Albatross judged on one question: does it produce money after costs, or intel that is correct, early and changes a decision. Nothing has been switched off; the suggestions run beside what they would replace.", art: "hero-reports" },
    avoid: { kicker: "Alpha · Loss avoidance", title: "Which calls to <em>skip</em>.", sub: "No rule that follows the group calls makes money. A few signals tell the worst losers apart, and skipping those is worth dollars. Paper only.", art: "hero-deep", pos: "70% 12%" },
    edge: { kicker: "Alpha · Factor edge", title: "Is the ranking still <em>working</em>?", sub: "Factor rank is the one ordering of the liquid coins that beats chance. Each week's accuracy, the ways to trade it, and what could trip it this week.", art: "hero-matrix" },
    lab: { kicker: "Results · Strategy lab", title: "Strategies <em>tested</em> every hour.", sub: "New memecoin and factor strategies, each tested on older data against a bar that rises with every one tried, then on recent data it never saw. Up to 20 survivors trade on paper, and only new data can make one a champion.", art: "hero-ideas" },
    // Memecoins: the Deep End
    deep: { kicker: "Memecoins · Feed", title: "The <em>deep</em> end.", sub: "Every memecoin on our radar with its evidence in one place: the calls, X, holders, security and the tape. \"No red flag\" only when all eight red-flag checks ran. The score is a count of the evidence and makes no forecast.", art: "hero-deep", pos: "70% 12%" },
    dossier: { kicker: "Memecoins · Dossier", title: "The <em>evidence</em>.", art: "hero-deep", pos: "70% 12%" },
    // Majors: factor rank first; the direction calls marked unproven
    factor: { kicker: "Majors · Factor rank", title: "Coins ranked <em>against each other</em>.", sub: "The liquid coins, ranked every day on three factors. Nothing in our data predicts where the whole market goes next.", art: "hero-matrix" },
    matrix: { kicker: "Majors · All coins · direction calls, unproven", title: "Every coin, <em>at a glance</em>.", sub: "The Strategist's call on the three timeframes that matter most. Click a coin for the full picture and the reasoning.", art: "hero-matrix" },
    sectors: { kicker: "Majors · Sectors · direction calls, unproven", title: "Where the <em>money</em> leans.", sub: "Each box is a sector, sized by its coins and coloured by its quant score on the chosen timeframe. The quant model grades worse than a coin flip, so read the colours as unproven.", art: "hero-sectors" },
    coin: { kicker: "Majors · Coin", title: "", art: "hero-matrix" },
    calendar: { kicker: "Majors · Calendar", title: "What's <em>coming</em>.", sub: "Listings, token launches, unlocks, upgrades and the macro dates that move markets, from the exchanges, Polymarket, the news and the Oracle's web sweep. Every entry links to where it was reported.", art: "hero-reports" },
    news: { kicker: "Majors · News", title: "The <em>wire</em>.", sub: "Every headline, scored for impact and direction by News Scout.", art: "hero-reports" },
    social: { kicker: "Majors · X", title: "What X is <em>saying</em>.", sub: "Grok's read of the crowd, every claim tied to posts it actually retrieved.", art: "hero-home" },
    // Ops: running the machine, in a plainer look
    agents: { kicker: "Ops · Agents", title: "Who reads <em>whom</em>.", sub: "Every agent and code step, left to right by how many model steps stand between it and raw data. Hover a card to trace what it reads and who reads it; open one for its full record.", art: "empty-calm" },
    output: { kicker: "Ops · Spend & allowance", title: "What the agents <em>made</em>.", sub: "Every agent's real output, what it cost and how it has graded, the ChatGPT allowance, the loops to watch, and a pause and budget control for each.", art: "empty-calm" },
    health: { kicker: "Ops · Health & alerts", title: "The <em>engine</em> room.", sub: "Alerts, jobs, data collected and what the agents spent today.", art: "empty-calm" },
    setup: { kicker: "Ops · Setup", title: "Your <em>keys</em>.", sub: "Connect the AI agents, X and Telegram, and add data keys. Everything is optional and applies without a restart.", art: "empty-calm" },
    micro: { kicker: "Ops · All tokens", title: "Every token, <em>priced</em>.", sub: "Every token called in the Telegram groups or traded by the bot, priced from its own pools and cross-checked. Open one for its full dossier.", art: "hero-deep", pos: "70% 12%" },
  };
  const REGIME_WORD = {
    trending_up: "climbing", trending_down: "falling", range: "ranging", pullback_in_uptrend: "pulling back",
    bounce_in_downtrend: "bouncing", breakout_attempt: "testing the ceiling", breakdown_attempt: "testing the floor",
    capitulation: "capitulating", euphoria: "euphoric", mixed: "undecided",
    aligned_up: "climbing", aligned_down: "falling", uptrend_short_term_flat: "trending up", downtrend_short_term_flat: "trending down",
    range_mixed: "undecided",   // derived from quant scores (app.js regime()) when the Strategist has no label
  };
  const S = { overview: null, mapTf: "1d", coin: null, matrix: null, dossier: null, answer: null };
  // A move that rounds to zero at the shown precision is flat: no arrow, no colour.
  const deltaChip = (v, d = 2, suffix = "%", title = "", pre = "") => {
    const r = v == null ? null : +v.toFixed(d), t = r == null || r === 0 ? "flat" : r > 0 ? "up" : "down";
    return `<span class="delta ${t}"${title ? ` title="${esc(title)}"` : ""}>${r == null ? "–" : `${{ up: "▲", down: "▼" }[t] || ""} ${pre}${Math.abs(r).toFixed(d)}${suffix}`}</span>`;
  };

  // ─── masthead ──────────────────────────────────────────────────────────────
  const regimeWord = (label) => (label && label !== "–" ? REGIME_WORD[label.toLowerCase().replace(/[^a-z]+/g, "_")] || label.toLowerCase() : null);
  function mast(tab) {
    const m = MAST[tab] || MAST.home;
    let kicker = typeof m.kicker === "function" ? m.kicker() : m.kicker, title = m.title, sub = m.sub || "";
    if (tab === "home" && S.answer) ({ kicker, title, sub } = { ...S.answer, kicker: S.answer.kicker || kicker });   // home.js
    if (tab === "matrix") {        // name whichever source and timeframes the table shows
      const mx = S.matrix, src = mx?.source || "strategist";
      const who = src === "ensemble" ? "The blended call" : `${{ strategist: "The Strategist", quant: "The quant model" }[src] || srcLabel(src)}'s call`;
      const gap = Object.keys(state.matrix?.uncovered || {});
      sub = `${who} on ${state.matrixMore ? (gap.length ? `every timeframe a source with skill covers (none covers ${gap.join(" or ")}), with funding and open interest`
        : "every timeframe, with funding and open interest") : "the three timeframes that matter most"}. Click a coin for the full picture and the reasoning.`;
      if (state.cat) {             // drilled in from a sector; the count waits for its rows
        const ready = mx?.cat === state.cat && mx.n > 0;     // no count before its rows, or for a sector that isn't one
        kicker = "Majors · Sector · direction calls, unproven";
        title = ready ? `${esc(catName(state.cat))}: <em>${mx.n}</em> coin${mx.n === 1 ? "" : "s"}.` : `${esc(catName(state.cat))}.`;
        if (!ready) sub = "";
        else if (!mx.searching) sub = !mx.rest ? `${who} has a call on every one. Click a coin for the reasoning.`
          : mx.called ? `${who} calls ${mx.called} of them; the other ${mx.rest} are listed under the table. Click a coin for the reasoning.`
          : `${who} has no call on any of them; they are listed under the table. Click a coin for its chart and indicators.`;
      }
    }
    if (tab === "coin") {
      const c = S.coin && S.coin.symbol === state.coin ? S.coin : null, w = regimeWord(c?.regime);
      title = `${esc(state.coin || "")}${w ? ` is <em>${esc(w)}</em>.` : ""}`;
      sub = c?.missing ? "Not a coin Albatross tracks."
        : c ? [c.name, `Tier ${c.tier}`, c.categories.slice(0, 3).join(", "), c.mcap ? `market cap ${fmtUsd(c.mcap)}` : ""].filter(Boolean).join(" · ") : "";
    }
    if (tab === "book") {           // a book's page (books.js) names the book and its rule
      const bk = S.book && S.book.key === state.book ? S.book : null;
      title = !bk ? "" : bk.missing ? "No such book." : `${esc(bk.name)}.`;
      sub = bk?.rule || "";
    }
    // A dossier (deep.js) names its token; the facts line leads with the price and its hour's move, like a coin page.
    const dz = tab === "dossier" && S.dossier && S.dossier.key === `${state.deep?.chain}/${state.deep?.address}` ? S.dossier : null;
    if (dz) {
      title = `The evidence on <em>${esc(dz.symbol)}</em>.`;
      sub = dz.facts.filter(Boolean).join(" · ");
    }
    $("#mast-kicker").textContent = kicker;
    $("#mast-title").innerHTML = title;
    $("#mast-sub").textContent = sub;
    const c = tab === "coin" && S.coin?.symbol === state.coin ? S.coin : null;
    if (c?.price != null) $("#mast-sub").insertAdjacentHTML("afterbegin",     // a coin page leads with its price
      `<span class="mast-px"><b>$${fmtPrice(c.price)}</b> ${deltaChip(c.chg)} <span>24h</span></span> · `);
    if (dz?.price) $("#mast-sub").insertAdjacentHTML("afterbegin",
      `<span class="mast-px"><b>$${esc(dz.price)}</b>${dz.chg == null ? "" : ` ${deltaChip(dz.chg, 1)} <span>1h</span>`}</span>${sub ? " · " : ""}`);
    $("#mast-art").style.backgroundImage = ART(m.art);
    $("#mast-art").style.backgroundPosition = m.pos || "";      // a painting framed off the default, where its subject sits
    const top = document.querySelector(".top");
    top.classList.toggle("compact", tab !== "home");
    top.dataset.sub = tab === "home" || tab === "coin" || tab === "dossier" ? "facts" : "about";   // phones drop the explanations
    // The Deep End is its own product: its pages say so in the tab title.
    const deep = tab === "deep" || tab === "micro" || tab === "dossier";
    const page = tab === "coin" ? state.coin : tab === "dossier" ? dz?.symbol || "Dossier" : tab === "deep" ? "" : tab === "matrix" && state.cat ? catName(state.cat)
      : tab === "book" ? state.bookName || "Book" : VIEW_NAME[tab];
    document.title = deep ? (page ? `${page} · The Deep End` : "The Deep End") : page ? `${page} · Albatross` : "Albatross";
  }
  const currentTab = () => state.tab || "home";
  document.addEventListener("viewchange", (e) => mast(e.detail.view));
  document.addEventListener("coinloaded", (e) => { S.coin = e.detail; if (currentTab() === "coin") mast("coin"); });
  document.addEventListener("matrixrendered", (e) => { S.matrix = e.detail; if (currentTab() === "matrix") mast("matrix"); });
  document.addEventListener("dossierloaded", (e) => { S.dossier = e.detail; if (currentTab() === "dossier") mast("dossier"); });
  document.addEventListener("homeanswer", (e) => { S.answer = e.detail; if (currentTab() === "home") mast("home"); });
  document.addEventListener("bookloaded", (e) => { S.book = e.detail; if (currentTab() === "book") mast("book"); });

  // ─── overview ─────────────────────────────────────────────────────────────
  // The opening scene plays in full once per browser session; a reload lifts the loader as soon as the data is in.
  let seen = false;
  try { seen = sessionStorage.getItem("albatross.seen") === "1"; sessionStorage.setItem("albatross.seen", "1"); } catch {}
  async function loadOverview() {
    S.overview = await get("/api/overview");
    renderKpis(); renderCompass(); renderRadar();
    if (seen) $("#loader").classList.add("gone");
  }

  function kpi(k, v, d, toneVar) {
    return `<div class="kpi" style="--tone:${toneVar || "var(--cream)"}"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
  }
  function renderKpis() {
    const { btc, eth, fear_greed: fg, macro: m } = S.overview.kpis;
    const fgWord = (x) => (x == null ? "" : x >= 75 ? "extreme greed" : x >= 55 ? "greed" : x > 45 ? "neutral" : x > 25 ? "fear" : "extreme fear");
    const flows = m.btc_etf_net_inflow_musd_5d;
    const session = m.btc_etf_date ? new Date(`${m.btc_etf_date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }) : "last session";
    const edge = (v) => (v == null ? "var(--cream)" : v < 0 ? "var(--bear)" : "var(--bull)");
    const mv = S.overview.kpis.mvrv || {}, bz = mv.btc;
    // BTC cycle zones from its own history: below 0 at the 2015/2018/2022 bottoms, 7+ at the 2013/2017 tops.
    const zone = (z) => (z < 0 ? "below realized value" : z < 2 ? "fair-value range" : z < 5 ? "heated" : z < 7 ? "overheated" : "historic top zone");
    $("#home-kpis").innerHTML = [
      kpi("Bitcoin", btc ? `$${fmtPrice(btc.price)}` : "–", `${deltaChip(btc?.chg_24h_pct)} <span>24h</span>`, edge(btc?.chg_24h_pct)),
      kpi("Ether", eth ? `$${fmtPrice(eth.price)}` : "–", `${deltaChip(eth?.chg_24h_pct)} <span>24h</span>`, edge(eth?.chg_24h_pct)),
      kpi("Fear & Greed", fg.value == null ? "–" : `${Math.round(fg.value)} <small>${fgWord(fg.value)}</small>`, `${deltaChip(fg.week_ago == null ? null : fg.value - fg.week_ago, 0, "")} <span>vs a week ago</span>`, "var(--warning)"),
      kpi("BTC ETF flows", flows == null ? "–" : `${flows >= 0 ? "+" : "−"}$${fmtUsd(Math.abs(flows) * 1e6).slice(1)}`, `<span>5 trading days ·</span> ${deltaChip(m.btc_etf_net_inflow_musd_1d, 0, "M", "Net flow in the latest US trading session", "$")} <span>${esc(session)}</span>`, edge(flows)),
      bz ? kpi("BTC MVRV Z", `${bz.z.toFixed(2)} <small>${zone(bz.z)}</small>`,
        `${deltaChip(bz.z_30d_ago == null ? null : bz.z - bz.z_30d_ago, 2, "", "Change over 30 days")} <span>30 days${mv.eth ? ` · ETH ${mv.eth.z.toFixed(2)}` : ""} · ${esc(new Date(`${bz.as_of}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }))}</span>`,
        bz.z < 0 ? "var(--good)" : bz.z >= 5 ? "var(--bear)" : "var(--cream)") : ""
    ].join("");
    const cyc = [...$("#home-kpis").children].find((k) => k.textContent.includes("MVRV"));
    if (cyc) {
      Object.assign(cyc, { tabIndex: 0, title: "Market value against realized value, in standard deviations. Open Bitcoin's page for the full cycle chart." });
      cyc.classList.add("kpi-link", "kpi-wide");
      cyc.setAttribute("role", "link");
      const go = () => openAsset("BTC");
      cyc.addEventListener("click", go);
      cyc.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    }
  }

  function renderCompass() {
    const rows = S.overview.compass;
    const tfs = ["1h", "4h", "1d"];
    const head = `<thead><tr><th>Coin</th><th>Price</th><th class="chg">24h</th>${tfs.map((t) => `<th>${t}</th>`).join("")}<th class="left reg">Regime</th></tr></thead>`;
    const body = rows.map((c) => `<tr data-sym="${esc(c.symbol)}"><td class="sym"><a href="#/coin/${encodeURIComponent(c.symbol)}" data-coin="${esc(c.symbol)}">${esc(c.symbol)}</a></td>
        <td class="px">${c.price ? fmtPrice(c.price) : "–"}<span class="chg-m">${deltaChip(c.chg_24h_pct)}</span></td><td class="chg">${deltaChip(c.chg_24h_pct)}</td>
        ${tfs.map((t) => cellHtml(c.tfs[t], null, c.tfs[t]?.thesis ? `<b>${esc(c.symbol)} · ${t}</b><div class="tip-text">${esc(c.tfs[t].thesis)}</div>` : null)).join("")}
        <td class="reg">${esc(humanize(c.regime))}</td></tr>`).join("");
    $("#home-compass").innerHTML = head + `<tbody>${body || `<tr><td colspan="7" class="left note">The Strategist hasn't called the majors yet.</td></tr>`}</tbody>`;
  }

  const KIND = { idea: "Idea", oracle: "Oracle", news: "Headline", alert: "Alert" };
  const TONE = { bull: "var(--bull)", bear: "var(--bear)", accent: "var(--cream)", flat: "var(--axis)" };
  const radarLink = (e) => (e.url ? `href="${esc(e.url)}" target="_blank" rel="noopener noreferrer"`
    : e.kind === "idea" ? `href="#/ideas" data-go="ideas"`
    : e.kind === "oracle" && e.scope ? `href="#/coin/${encodeURIComponent(e.scope)}" data-coin="${esc(e.scope)}"`
    : e.kind === "alert" ? `href="#/health" data-go="health"` : "");
  function renderRadar() {
    const items = S.overview.radar.slice(0, 6);
    $("#home-radar").innerHTML = items.length ? items.map((e) => `<li style="--tone:${TONE[e.tone] || TONE.flat}">
        <div class="when">${esc(ago(e.at))}</div>
        <div><div class="kind">${e.n ? "Ideas" : KIND[e.kind] || e.kind}</div>
          <div class="t">${radarLink(e) ? `<a ${radarLink(e)}>${esc(e.title)}${e.url ? `<span class="ext" aria-label="opens in a new tab"> ↗</span>` : ""}</a>` : esc(e.title)}</div>
          ${e.detail ? `<div class="dd">${esc(e.detail)}</div>` : ""}</div></li>`).join("")
      : `<li class="empty">A quiet day: no ideas, escalations, big headlines or alerts in the last 24 hours.</li>`;
  }

  // ─── sector treemap (squarified) ───────────────────────────────────────────
  function squarify(items, x, y, w, h, out = []) {
    if (!items.length) return out;
    const total = items.reduce((a, b) => a + b.v, 0);
    const area = w * h, scale = area / total;
    const short = Math.min(w, h);
    let rowItems = [], best = Infinity, i = 0;
    const worst = (row) => {
      const s = row.reduce((a, b) => a + b.v * scale, 0), mx = Math.max(...row.map((r) => r.v * scale)), mn = Math.min(...row.map((r) => r.v * scale));
      return Math.max((short * short * mx) / (s * s), (s * s) / (short * short * mn));
    };
    while (i < items.length) {
      const trial = [...rowItems, items[i]], wv = worst(trial);
      if (wv > best && rowItems.length) break;
      rowItems = trial; best = wv; i++;
    }
    const s = rowItems.reduce((a, b) => a + b.v * scale, 0), thick = s / short;
    let off = 0;
    for (const it of rowItems) {
      const len = (it.v * scale) / thick;
      out.push(w >= h ? { ...it, x, y: y + off, w: thick, h: len } : { ...it, x: x + off, y, w: len, h: thick });
      off += len;
    }
    return w >= h ? squarify(items.slice(i), x + thick, y, w - thick, h, out) : squarify(items.slice(i), x, y + thick, w, h - thick, out);
  }
  function renderTreemap() {
    const host = $("#sectors-map");
    if (!host || !state.sectors) return;
    const W = host.clientWidth, H = host.clientHeight;
    // Sized by the coins a click lists; coloured by the sector's quant score on the chosen timeframe.
    const items = state.sectors.sectors.map((s) => ({ name: s.name, slug: s.slug, v: Math.max(1, s.coins || 0), sig: s.tfs[S.mapTf] }))
      .sort((a, b) => b.v - a.v);
    host.innerHTML = squarify(items, 0, 0, W, H).map((r) => {
      const sc = r.sig?.score, pole = sc < 0 ? "var(--bear)" : "var(--bull)";
      const mag = Math.min(1, Math.abs(sc ?? 0) * 1.6);
      const score = sc == null ? "no call" : `${sc > 0 ? "+" : sc < 0 ? "−" : ""}${Math.abs(sc).toFixed(2)}`;   // signed score; ▲/▼ is kept for calls
      // The label shrinks with its box: name, score and coins; name and score; the name alone; nothing.
      const fit = r.w >= 118 && r.h >= 70 ? "l" : r.w >= 76 && r.h >= 50 ? "m" : r.w >= 44 && r.h >= 26 ? "s" : "xs";
      // A long name wraps only where it has the room, and never mid-word (~7.5 px a character).
      const longest = Math.max(...r.name.split(/\s+/).map((x) => x.length));
      const lines = fit !== "s" && r.h >= (fit === "l" ? 84 : 64) && longest * 7.5 <= r.w - 20 ? 2 : 1;
      const label = fit === "xs" ? "" : `<b>${esc(r.name)}</b>` + (fit === "s" ? "" : `<span class="tm-sc">${score}</span>`) +
        (fit === "l" ? `<span class="tm-n">${r.v} coins</span>` : "");
      const desc = `${r.name}: ${r.v} coins, ${S.mapTf} quant score ${sc == null ? "none" : (sc > 0 ? "+" : "") + sc.toFixed(2)}`;
      return `<div class="tm ${fit}${lines === 1 ? " one" : ""}" role="button" tabindex="0" data-sector="${esc(r.slug)}" aria-label="${esc(desc)}" title="${esc(desc)} · click for its coins"
        style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;--c:color-mix(in oklab, ${pole} ${(mag * 100).toFixed(0)}%, var(--mid))">${label}</div>`;
    }).join("");
  }
  new MutationObserver(renderTreemap).observe($("#sectors"), { childList: true });
  addEventListener("resize", () => { if (currentTab() === "sectors") renderTreemap(); });

  // ─── wiring ────────────────────────────────────────────────────────────────
  const chipGroup = (sel, key, fn) => $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-" + key + "]");
    if (!b) return;
    $(sel).querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    fn(b.dataset[key]);
  });
  $("#sectors-map").addEventListener("click", (e) => { const t = e.target.closest("[data-sector]"); if (t) openSector(t.dataset.sector); });
  $("#sectors-map").addEventListener("keydown", (e) => { const t = e.target.closest("[data-sector]"); if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSector(t.dataset.sector); } });
  chipGroup("#map-tf", "tf", (v) => { S.mapTf = v; renderTreemap(); });
  document.addEventListener("click", (e) => {
    const go = e.target.closest("[data-go]");
    if (go) {
      e.preventDefault();
      if (go.getAttribute("href")?.startsWith("#/report/")) state.reportId = Number(go.getAttribute("href").slice(9));
      if (go.dataset.go === "matrix") setCat("");      // "every coin" means every coin, not the last sector opened
      setTab(go.dataset.go); scrollTo({ top: 0 });
    }
    const tr = e.target.closest("#home-compass tr[data-sym]");
    if (tr) openAsset(tr.dataset.sym);
  });
  // Coin links on Today (Compass names, Oracle entries) open the coin page here; the href serves new tabs.
  $("#tab-home").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-coin]");
    if (a) { e.preventDefault(); e.stopPropagation(); openAsset(a.dataset.coin); }
  });

  // Mini loader: count in-flight API requests; paint the brush stroke along the top if they take over 250 ms.
  const brush = $("#brush");
  let inflight = 0, brushTimer = null;
  const origFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const api = String(args[0]).includes("/api/");
    if (api && ++inflight === 1) brushTimer = setTimeout(() => brush.classList.add("on"), 250);
    try { return await origFetch(...args); }
    finally { if (api && --inflight === 0) { clearTimeout(brushTimer); brush.classList.remove("on"); } }
  };

  document.querySelectorAll("main .tab > p.note, main .tab > div > p.note").forEach((p) => {
    if (p.closest("details") || p.id === "isc-note") return;
    const d = document.createElement("details");
    d.className = "note-fold";
    d.innerHTML = "<summary>How to read this</summary>";
    p.replaceWith(d); d.append(p);
  });

  const loader = $("#loader");
  const started = Date.now();
  // Hold the opening scene long enough to be seen (1.8 s), even when the data is instant.
  const dismiss = () => setTimeout(() => loader.classList.add("gone"), Math.max(0, 1800 - (Date.now() - started)));
  // Guarded like every loader in app.js: a failed overview says so in its panels instead of leaving them blank.
  loadOverview = guarded("the market overview", ["#home-compass", "#home-radar"], loadOverview);
  loadOverview().finally(dismiss);
  setTimeout(dismiss, 5000);                          // never trap the page behind the loader
  // the market tiles and what changed (Factor rank), the ten majors (All coins): app.js's minute tick
  document.addEventListener("refresh", (e) => { if (["factor", "matrix"].includes(e.detail.view)) loadOverview(); });
  mast(currentTab());
})();
