"use strict";
// The Deep End (docs/SPEC-DEEP-END.md): the feed (#/deep) and a token's dossier (#/deep/<chain>/<address>).
// The evidence engine behind /api/deep/* is built in parallel. Until it answers, the feed and the dossier fall back to the
// micro-cap price pipeline (/api/micro/*) and say plainly which evidence is missing: missing is never "good".
// Uses app.js helpers: $, esc, get, ago, utc, tipId, tips, whyFailed, cap, retries, safeUrl, setTab, state,
// renderSubnav, moodDab, moodWord.
(() => {
  const feedTab = $("#tab-deep"), dossTab = $("#tab-dossier");
  if (!feedTab || !dossTab) return;
  // TradingView Lightweight Charts, pinned and checked (subresource integrity), loaded the first time a dossier opens.
  const LWC_SRC = "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js";
  const LWC_SRI = "sha384-OK7vELvjHdhUFi31JYioPIcRHTROLdcDa6ZsNWgvgLaKj+9JqhU0Ad8g4wz3CXjA";
  const PHONE = matchMedia("(max-width: 760px)");
  const CALM = matchMedia("(prefers-reduced-motion: reduce)");
  const enc = encodeURIComponent;

  // ─── formatting ──────────────────────────────────────────────────────────
  // Micro-cap prices: 0.0₄6894 = 0.00006894 (the subscript counts the zeros after the point), as on the token list.
  const SUB = "₀₁₂₃₄₅₆₇₈₉";
  function price(p) {
    if (p == null || !isFinite(p)) return "–";
    if (p <= 0) return "0";
    if (p >= 1) return p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 0 : 3 });
    if (p >= 0.001) return p.toPrecision(4);
    const [m, e] = p.toExponential(3).split("e");
    return `0.0${String(-Number(e) - 1).replace(/\d/g, (d) => SUB[d])}${m.replace(".", "")}`;
  }
  // Set in the serif face, the zero count is a real subscript: Instrument Serif draws "₃" nearly full size, and 0.0₃5369
  // must never read as 0.035369. Small sans and mono text keeps the Unicode subscript, as the token list does.
  const priceHtml = (p) => esc(price(p)).replace(/[₀-₉]+/g, (z) => `<sub class="dz-z">${[...z].map((c) => SUB.indexOf(c)).join("")}</sub>`);
  const usd = (v) => (v == null || !isFinite(v) ? "–" : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M`
    : v >= 1e4 ? `$${Math.round(v / 1e3)}K` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}K` : `$${Math.round(v)}`);
  const signedUsd = (v) => (v == null ? "–" : `${v >= 0 ? "+" : "−"}${usd(Math.abs(v))}`);
  const dur = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m`
    : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`);
  const short = (a) => (a && a.length > 14 ? `${a.slice(0, 5)}…${a.slice(-4)}` : a || "");
  const int = (n) => (n == null || !isFinite(n) ? "–" : Math.round(n).toLocaleString());
  const plural = (n, one, many = `${one}s`) => `${int(n)} ${n === 1 ? one : many}`;
  const pct = (v, d = 1) => (v == null || !isFinite(v) ? "–" : `${(+v).toFixed(d)}%`);
  const ms = (ts) => (ts ? +new Date(ts) : null);
  const sinceFmt = (v) => (v == null ? "–" : v >= 1000 ? `${(v / 100 + 1).toFixed(0)}×`
    : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 ? 1 : 0)}%`);
  const xs = (v) => (v == null ? "–" : v >= 10 ? `${v.toFixed(0)}×` : `${v.toFixed(v >= 2 ? 1 : 2)}×`);
  const upDown = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
  const hhmm = (sec) => new Date(sec * 1000).toISOString().slice(11, 16);
  const when = (ts) => (ts ? `<span title="${esc(utc(ts))}">${esc(ago(ts))}</span>` : "–");
  // A filled chip with its arrow (a move that rounds to zero is flat: no arrow, no colour), as on the rest of the site.
  function delta(v, d = 1, per = "") {
    const r = v == null || !isFinite(v) ? null : +(+v).toFixed(d), t = r == null || r === 0 ? "flat" : r > 0 ? "up" : "down";
    const n = r == null ? "–" : Math.abs(r) >= 1000 ? Math.round(Math.abs(r)).toLocaleString() : Math.abs(r).toFixed(d);
    return `<span class="delta ${t}">${t === "up" ? "▲ " : t === "down" ? "▼ " : ""}${n}${r == null ? "" : "%"}</span>${per ? ` <span class="dz-per">${per}</span>` : ""}`;
  }
  const asOf = (ts, word = "as of") => (ts ? `<span class="dz-asof" title="${esc(utc(ts))}">${word} ${esc(ago(ts))}</span>` : "");

  const CHAIN = { solana: "SOL", base: "BASE", bsc: "BSC", ethereum: "ETH", robinhood: "RH", evm: "EVM?" };
  const CHAIN_NAME = { solana: "Solana", base: "Base", bsc: "BNB Chain", ethereum: "Ethereum", robinhood: "Robinhood", evm: "Chain not found yet" };
  const CHAINS = ["solana", "base", "bsc", "ethereum", "robinhood"];
  const EXPLORER = { solana: ["Solscan", "https://solscan.io"], bsc: ["BscScan", "https://bscscan.com"],
    ethereum: ["Etherscan", "https://etherscan.io"], base: ["BaseScan", "https://basescan.org"] };
  const DEXSCREENER = { solana: "solana", bsc: "bsc", ethereum: "ethereum", base: "base" };
  const walletUrl = (chain, a) => (EXPLORER[chain] && a ? `${EXPLORER[chain][1]}/${chain === "solana" ? "account" : "address"}/${a}` : null);
  const chainChip = (c) => `<span class="mc-chain" title="${esc(CHAIN_NAME[c] || c)}">${esc(CHAIN[c] || c)}</span>`;

  // A token's logo, or its initials on a flat colour picked from its address (never text on a painted texture).
  const HUES = ["#2f5f92", "#8a3a33", "#665527", "#3b5a41", "#524675", "#2c585b"];
  const hue = (s) => HUES[[...String(s)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % HUES.length];
  function logo(t, cls = "") {
    const sym = String(t.symbol || t.name || t.address || "?").replace(/^\$/, "").trim() || "?";
    const chars = [...sym], latin = /^[\x21-\x7e]/.test(sym);
    const src = githubHosted(t.logo) ? null : safeUrl(t.logo);      // never an image from GitHub (app.js)
    return `<span class="dz-logo${cls ? ` ${cls}` : ""}" style="--lg:${hue(t.address || sym)}" aria-hidden="true"><span>${esc(latin ? chars.slice(0, 2).join("").toUpperCase() : chars[0])}</span>${
      src ? `<img src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}</span>`;
  }
  // A long FOMO thesis opens in place. Its "Read all" shows only where the four-line clamp actually cuts the text,
  // measured once the card is laid out (a folded card on a phone is measured when it opens).
  function fitTheses(root = document) {
    root.querySelectorAll(".dz-thesis:not(.open)").forEach((li) => {
      const p = li.querySelector("p"), b = li.querySelector(".dz-readall");
      if (p && b && p.offsetParent !== null) b.hidden = p.scrollHeight <= p.clientHeight + 1;
    });
  }
  document.addEventListener("toggle", (e) => { if (e.target instanceof HTMLDetailsElement && e.target.open) fitTheses(e.target); }, true);
  document.addEventListener("click", (e) => {
    const b = e.target instanceof Element ? e.target.closest(".dz-readall") : null;
    if (!b) return;
    const open = b.closest(".dz-thesis").classList.toggle("open");
    b.setAttribute("aria-expanded", String(open));
    b.textContent = open ? "Read less" : "Read all";
  });
  // A logo that fails to load leaves the initials underneath.
  document.addEventListener("error", (e) => { if (e.target instanceof HTMLImageElement && e.target.closest(".dz-logo")) e.target.remove(); }, true);

  // The evidence tally, small: the score, the lines as a stacked bar (good, warn, bad, then unknown as the track) and the
  // counts with their glyphs, so colour is never the only cue.
  // The score is null until enough lines are known (the engine needs five): the counts still show, the number doesn't.
  // One rule everywhere (as on the dossier): ✕ counts red flags, the failed red-flag checks; a line that failed
  // without being a red-flag check counts with the cautions.
  const scored = (sc) => sc?.value != null && isFinite(sc.value);
  const byRule = (sc, flags) => {
    const red = Math.min(flags ?? sc.bad ?? 0, sc.bad || 0);
    return { ...sc, bad: red, warn: (sc.warn || 0) + (sc.bad || 0) - red };
  };
  function tally(sc0, flags) {
    if (!sc0) return `<span class="dz-tally none">–</span>`;
    const sc = byRule(sc0, flags);
    const seg = (k) => (sc[k] ? `<i class="t-${k}" style="flex-grow:${sc[k]}"></i>` : "");
    const cnt = (k, g) => `<span class="dz-cnt c-${k}"><i>${g}</i>${sc[k] || 0}</span>`;
    return `<span class="dz-tally" role="img" aria-label="${scored(sc) ? `Evidence score ${Math.round(sc.value)} of 100` : "No score yet"}: ${tallyWords(sc)}">
      <b class="dz-score${scored(sc) ? "" : " none"}">${scored(sc) ? Math.round(sc.value) : "–"}</b><span class="dz-tbar">${seg("good")}${seg("warn")}${seg("bad")}${seg("unknown")}</span>
      <span class="dz-cnts">${cnt("good", "✓")}${cnt("warn", "!")}${cnt("bad", "✕")}${sc.unknown ? cnt("unknown", "?") : ""}</span></span>`;
  }
  const tallyWords = (sc) => `${sc.good || 0} good, ${sc.warn || 0} ${sc.warn === 1 ? "caution" : "cautions"}, ${sc.bad || 0} ${sc.bad === 1 ? "red flag" : "red flags"}, ${sc.unknown || 0} unknown`;

  // The feed's small chart: closes since just before the first call, the call price dashed, a dot where the call came in.
  function spark(r, W = 104, H = 30) {
    const s = r.spark;
    if (!s?.v?.some((v) => v != null) || s.v.length < 2) return `<span class="cat">–</span>`;
    let last = s.before ?? s.v.find((v) => v != null);
    const vals = s.v.map((v) => (v == null ? last : (last = v)));
    const cp = s.call_price, nums = [...vals, ...(cp ? [cp] : [])].filter((v) => v > 0);
    if (!nums.length) return `<span class="cat">–</span>`;
    const lo = Math.min(...nums), hi = Math.max(...nums), log = hi / lo > 3, f = log ? Math.log : (v) => v;
    const P = 3, span = f(hi) - f(lo) || 1;
    const y = (v) => P + (1 - (f(v) - f(lo)) / span) * (H - 2 * P);
    const x = (i) => P + (i / (vals.length - 1)) * (W - 2 * P);
    const pts = vals.map((v, i) => (v > 0 ? `${x(i).toFixed(1)},${y(v).toFixed(1)}` : null)).filter(Boolean).join(" ");
    const end = vals[vals.length - 1], first = vals.find((v) => v > 0);
    const up = r.since_call_pct != null ? r.since_call_pct >= 0 : end >= first;
    const t0 = ms(s.t0), t1 = ms(s.t1), ct = ms(r.first_call_at);
    const cx = cp && ct && t1 > t0 ? P + Math.max(0, Math.min(1, (ct - t0) / (t1 - t0))) * (W - 2 * P) : null;
    return `<svg class="mc-spark ${up ? "up" : "down"}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
      ${cp ? `<line class="lvl" x1="${P}" x2="${W - P}" y1="${y(cp).toFixed(1)}" y2="${y(cp).toFixed(1)}"/>` : ""}
      <polyline points="${pts}"/>${cx != null ? `<circle cx="${cx.toFixed(1)}" cy="${y(cp).toFixed(1)}" r="2.6"/>` : ""}</svg>`;
  }

  // While the evidence engine is down (any answer but a 404, which is about one token), the feed and the dossier stop
  // asking it for two minutes and use the price pipeline; each ask it turns away is a line in the browser console.
  const ENGINE = { downAt: 0, why: null };
  const engineUp = () => !ENGINE.downAt || Date.now() - ENGINE.downAt > 120_000;
  const engineFailed = (e) => { if (e.status !== 404) Object.assign(ENGINE, { downAt: Date.now(), why: e }); };

  // ═══ the feed ════════════════════════════════════════════════════════════
  const PAGE = 60;
  const F = { view: "hot", chain: "", clean: false, sort: null, dir: -1, data: null, mode: null, why: null, error: null, at: 0,
    seq: 0, micro: null, shown: PAGE, scroll: 0, tips: [] };
  const VIEWS = [
    ["hot", "Hot", "Called in the last 2 hours, called twice in the last hour, held by the bot, trending, or opened in the last 10 minutes"],
    ["new", "New", "New listings and tokens under a day old"],
    ["called", "Called", "Called in the Telegram groups in the last 24 hours"],
    ["trending", "Trending", "Trending on Birdeye and DexScreener now"],
  ];
  // Columns: [key, label, what it means, sort value (null = not sortable), first click sorts ascending].
  const COLS = [
    ["tok", "Token", "Symbol, chain, age since its first pool, and how it came onto our radar", (r) => r.age_s, true],
    ["px", "Price", "Price now and its change over the last hour", (r) => r.change_h1],
    ["since", "Since call", "Price now against the price at the first group call; peak = the highest since, as a multiple of the call price", (r) => r.since_call_pct],
    ["spark", "Chart", "Price since just before the first call. Dashed line = the call price, dot = the call", null],
    ["calls", "Calls", "Group calls: all of them, from how many groups, and how many in the last 24 hours", (r) => r.calls_24h],
    ["x", "X", "X heat from the X Scout: mood (−5 to +5), posts an hour and a heat label", (r) => r.x_heat?.posts_h],
    ["ev", "Evidence", "The dossier's checklist tallied: the 0–100 score and its good, warn, bad and unknown lines. A tally of evidence, not a prediction", (r) => r.score?.value],
    ["flags", "Red flags", "The eight red-flag checks every token gets: any that failed, named. \"No red flag\" only when all eight ran", (r) => (r.red_flags ? r.red_flags.length : null)],
    ["cap", "Mcap · liq", "Market cap, and the liquidity in its pools", (r) => r.mcap],
  ];

  feedTab.innerHTML = `
    <div class="dz-search" role="search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="dz-q" type="search" placeholder="Ticker, name or contract address" aria-label="Find a token by its contract address, ticker or name"
        autocomplete="off" spellcheck="false" aria-controls="dz-sr">
      <div class="dz-sr" id="dz-sr" hidden></div>
    </div>
    <div class="filters dz-filters">
      <div class="chips" id="dz-view" role="group" aria-label="Which tokens">${VIEWS.map(([v, label, title]) =>
        `<button type="button" data-view="${v}" title="${esc(title)}"${v === F.view ? ` class="on" aria-pressed="true"` : ` aria-pressed="false"`}>${label}</button>`).join("")}</div>
      <select id="dz-chain" aria-label="Chain"><option value="">All chains</option>${CHAINS.map((c) => `<option value="${c}">${CHAIN_NAME[c]}</option>`).join("")}</select>
      <label class="cal-toggle dz-clean" title="Only tokens where all eight red-flag checks ran and none failed"><input type="checkbox" id="dz-clean"> No red flags</label>
    </div>
    <div class="dz-status" id="dz-status" role="status"></div>
    <div class="table-wrap dz-feed-wrap"><table class="dz-feed" id="dz-feed"></table></div>
    <div class="mc-foot" id="dz-foot"></div>
    <details class="note-fold"><summary>How to read this</summary>
      <p class="note">The feed lists the tokens on our radar: group calls from the Telegram listeners, Birdeye trending and new
      listings, DexScreener boosts. <b>Evidence</b> is the dossier's checklist tallied (security, holders, flow, calls, X, culture
      fit): each line is good, warn, bad or unknown, and missing data counts as unknown, never good. The score is a tally of that
      evidence, not a prediction: it hasn't been graded against outcomes yet. <b>Red flags</b> come from the eight red-flag
      checks every token gets (mint and freeze, liquidity lock, deployer history, insiders, top-10 holders, transfer tax,
      liquidity and organic volume). A check that never ran is not a pass: "no red flag" shows only when all eight ran,
      otherwise the row says how many did.
      <b>X</b> is the X Scout's latest read: mood from −5 to +5 and posts an hour. Prices use a subscript for leading zeros:
      0.0₄68 = 0.000068. Click a token for its dossier.</p></details>`;

  const trackTip = (html) => { const id = tipId(html); F.tips.push(id); return id; };

  // The contract's rows (§4), or rows built from the price pipeline's token list while the evidence engine isn't answering.
  function microRows() {
    const now = Date.now(), H1 = 3600e3, sp = F.micro.sparks || {};
    const rows = F.micro.tokens.filter((t) => t.chain !== "evm" && (!F.chain || t.chain === F.chain)).map((t) => {
      const px = t.price_usd ?? t.last_price_usd;
      return { chain: t.chain, address: t.address, symbol: t.symbol, name: t.name, logo: null, age_s: t.age_s, price: px,
        stale: !!t.stale && px != null, mcap: t.mcap_usd, liquidity: t.liquidity_usd, change_h1: null,
        since_call_pct: t.call_price && px ? (px / t.call_price - 1) * 100 : null, peak_x: t.peak_x, n_calls: t.n_calls,
        n_groups: t.n_groups, calls_24h: t.n_calls_24h, first_call_at: t.call_at || t.first_call, last_call: t.last_call,
        held: t.n_open > 0, x_heat: null, score: null, red_flags: null,
        discovered_via: [{ call: "group call", fomo: "FOMO top trader" }[t.first_seen_via] || "bot trade"],
        spark: sp[`${t.chain}/${t.address}`] ? { ...sp[`${t.chain}/${t.address}`], call_price: t.call_price } : null };
    });
    const act = (r) => ms(r.last_call) || 0;
    const pick = {
      hot: (r) => now - (ms(r.first_call_at) || 0) < 2 * H1 || r.held || (r.calls_24h >= 2 && now - act(r) < H1),
      new: (r) => r.age_s != null && r.age_s < 86400,
      called: (r) => r.calls_24h > 0,
      trending: () => false,           // Birdeye trending comes through the evidence engine only
    }[F.view];
    const order = F.view === "new" ? (a, b) => a.age_s - b.age_s : (a, b) => act(b) - act(a);
    return rows.filter(pick).sort(order);
  }

  async function loadFeed() {
    const seq = ++F.seq, q = new URLSearchParams({ view: F.view });
    if (F.chain) q.set("chain", F.chain);
    if (engineUp()) {
      try {
        const d = await get(`/api/deep/feed?${q}`);
        ENGINE.downAt = 0;
        if (seq !== F.seq) return;
        Object.assign(F, { data: d, mode: "deep", why: null, error: null, at: Date.now() });
        renderFeed();
        return;
      } catch (e) {
        engineFailed(e);
        if (seq !== F.seq) return;
        F.why = e;
      }
    } else F.why = ENGINE.why;
    try {
      if (!F.micro || Date.now() - F.micro.at > 15_000) {
        const [m, sp] = await Promise.all([get("/api/micro/tokens?status=live"),
          !F.micro || Date.now() - F.micro.sparksAt > 60_000 ? get("/api/micro/sparks?status=live").catch(() => null) : null]);
        F.micro = { tokens: m.tokens, at: Date.now(), sparks: sp ? sp.sparks : F.micro?.sparks || {},
          sparksAt: sp ? Date.now() : F.micro?.sparksAt || 0 };
      }
      if (seq !== F.seq) return;
      Object.assign(F, { mode: "fallback", error: null, at: Date.now() });
    } catch (e) {
      if (seq !== F.seq) return;
      F.error = whyFailed(e);
    }
    renderFeed();
  }

  // "No red flag" only when every one of the eight red-flag checks ran: a check that never ran is not a pass.
  const fullyChecked = (r) => !!r.flag_checks && r.flag_checks.ran >= r.flag_checks.of;
  function flagCoverage(fc) {
    if (!fc || !fc.ran) return `<span class="dz-unchecked" title="None of the eight red-flag checks has run on this token yet">not checked</span>`;
    if (fc.ran < fc.of) return `<span class="dz-partial" title="${fc.of - fc.ran} of the ${fc.of} red-flag checks haven't run, so this is not a clean bill">${fc.ran} of ${fc.of} checked · none failed yet</span>`;
    return `<span class="dz-noflag" title="All ${fc.of} red-flag checks ran and none failed">no red flag</span>`;
  }

  function feedRows() {
    let rows = F.mode === "fallback" && F.micro ? microRows() : F.mode === "deep" ? (F.data?.rows || []) : [];
    if (F.chain) rows = rows.filter((r) => r.chain === F.chain);      // the server filters too; this keeps a stale answer honest
    if (F.clean && F.mode === "deep") rows = rows.filter((r) => r.red_flags && !r.red_flags.length && fullyChecked(r));
    const col = F.sort && COLS.find((c) => c[0] === F.sort);
    if (col?.[3]) {
      const v = (r) => col[3](r) ?? null;
      rows = [...rows].sort((a, b) => { const x = v(a), y = v(b); return x == null || y == null ? (x == null) - (y == null) : (x - y) * F.dir; });
    }
    return rows;
  }

  function renderFeed() {
    const table = $("#dz-feed"), status = $("#dz-status"), foot = $("#dz-foot");
    feedTab.querySelectorAll("#dz-view button").forEach((b) => { b.classList.toggle("on", b.dataset.view === F.view); b.setAttribute("aria-pressed", b.dataset.view === F.view); });
    $("#dz-chain").value = F.chain;
    const fallback = F.mode === "fallback", clean = $("#dz-clean");
    clean.checked = F.clean && !fallback;
    clean.disabled = fallback;
    clean.closest("label").title = fallback ? "Red flags aren't checked until the evidence engine is live" : "Only tokens where all eight red-flag checks ran and none failed";
    if (!F.mode) {
      status.innerHTML = F.error ? `<div class="load-state failed"><b>Couldn't load the Deep End.</b> <span class="msg">${esc(cap(F.error))}. Retrying automatically.</span>
        <button class="load-retry" data-retry="deep">Try now</button></div>`
        : `<div class="load-state loading"><span class="msg">Reading the trenches…</span></div>`;
      table.innerHTML = ""; foot.innerHTML = "";
      return;
    }
    F.tips.forEach((id) => tips.delete(id)); F.tips = [];
    const rows = feedRows(), shown = rows.slice(0, F.shown);
    const why = F.why ? (F.why.status === 503 ? "it answered “being built” (HTTP 503)" : whyFailed(F.why)) : "";
    status.innerHTML = [
      fallback ? `<div class="dz-banner"><span><b>The evidence engine isn't live yet</b>: ${esc(why)}. Until it is, this is the price
        pipeline's list: called tokens with their price and move since the call. Evidence scores, X heat and red flags aren't checked yet.</span></div>` : "",
      F.error ? `<div class="load-note"><span><b>Couldn't refresh the feed:</b> ${esc(F.error)}. Showing the list from ${esc(ago(F.at))}; retrying automatically.</span>
        <button class="load-retry" data-retry="deep">Try now</button></div>` : "",
    ].join("");
    const head = `<thead><tr>${COLS.map(([k, label, title, v]) => `<th class="dz-h-${k}${k === "tok" || k === "flags" ? " left" : ""}${v ? " dz-sortable" : ""}${F.sort === k ? " sorted" : ""}" title="${esc(title)}"${v ? ` data-k="${k}"` : ""}${
      F.sort === k ? ` aria-sort="${F.dir > 0 ? "ascending" : "descending"}"` : ""}>${label}${F.sort === k ? (F.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead>`;
    const body = shown.map(feedRow).join("");
    table.innerHTML = head + `<tbody>${body || `<tr class="dz-none-row"><td colspan="${COLS.length}">${emptyFeed()}</td></tr>`}</tbody>`;
    foot.innerHTML = rows.length ? `<span class="cat">${rows.length > F.shown ? `The first ${F.shown} of ${rows.length} tokens` : plural(rows.length, "token")}${
      fallback ? " from the price pipeline" : ""} · updated ${esc(ago(F.at))}. Click one for its dossier.</span>
      ${rows.length > F.shown ? `<button class="more-toggle dz-more">Show ${Math.min(PAGE, rows.length - F.shown)} more</button>` : ""}` : "";
  }

  function emptyFeed() {
    const fallback = F.mode === "fallback";
    const msg = fallback && F.view === "trending" ? ["Trending comes from Birdeye and DexScreener through the evidence engine.", "It shows here once the engine is live."]
      : F.clean ? ["Every token in this view has at least one red flag.", ""]
      : F.chain ? [`Nothing ${F.view === "new" ? "new" : F.view} on ${CHAIN_NAME[F.chain]} right now.`, ""]
      : { hot: ["Nothing is hot right now.", "A token turns hot when it is called in the last two hours, called twice in an hour, held by the bot, trending, or opened here."],
        new: ["No new tokens right now.", ""], called: ["No group calls in the last 24 hours.", ""], trending: ["Nothing trending right now.", ""] }[F.view];
    const reset = F.clean || F.chain ? `<button class="more-toggle dz-reset">Show every chain${F.clean ? ", red flags too" : ""}</button>` : "";
    return `<div class="dz-empty-feed"><img src="/static/art/albatross-cutout.webp" alt="" loading="lazy"><div><p class="dz-empty-t">${esc(msg[0])}</p>
      ${msg[1] ? `<p>${esc(msg[1])}</p>` : ""}${reset}</div></div>`;
  }

  function feedRow(r) {
    const k = `${r.chain}/${r.address}`, sym = r.symbol || short(r.address);
    const nm = r.name && r.name.trim().toLowerCase() !== (r.symbol || "").trim().toLowerCase() ? r.name : "";
    const via = r.discovered_via?.length ? `via ${r.discovered_via.join(", ")}` : "";
    const tok = `<td class="dz-c-tok"><a class="dz-tok" href="#/deep/${esc(r.chain)}/${esc(r.address)}" data-dossier="${esc(k)}" data-label="${esc(sym)}">${logo(r)}
        <span class="dz-tok-t"><span class="dz-l1"><b class="sym">${esc(sym)}</b>${chainChip(r.chain)}${r.age_s != null ? `<span class="dz-age" title="Age: since its first pool">${dur(r.age_s)}</span>` : ""}</span>
        <span class="dz-l2"${via ? ` title="${esc(`${nm || r.address} · ${via}`)}"` : ""}>${nm ? esc(nm) : `<span class="mono">${esc(short(r.address))}</span>`}${via ? `<span class="dz-via"> · ${esc(via)}</span>` : ""}</span></span></a></td>`;
    const px = `<td class="dz-c-px"><span class="dz-px${r.stale ? " mc-dim" : ""}">${price(r.price)}</span>${r.stale ? ` <span class="mc-stale">stale</span>` : ""}
        ${r.change_h1 != null ? `<span class="dz-pxchg">${delta(r.change_h1, 1, "1h")}</span>` : ""}</td>`;
    const v = r.since_call_pct;
    const since = `<td class="dz-c-since"><span class="dz-since1">${v == null ? `<span class="cat">${r.first_call_at ? "no price at the call" : "never called"}</span>`
      : `<span class="mc-since ${upDown(v)}">${sinceFmt(v)}</span>`}${r.peak_x ? ` <span class="dz-peak">peak ${xs(r.peak_x)}</span>` : ""}</span>${
      r.first_call_at ? `<span class="dz-sub" title="${esc(utc(r.first_call_at))}">called ${esc(ago(r.first_call_at))}</span>` : ""}</td>`;
    const calls = `<td class="dz-c-calls">${r.n_calls ? `<b>${int(r.n_calls)}</b> ${r.n_calls === 1 ? "call" : "calls"}<span class="dz-sub">${plural(r.n_groups || 0, "group")} · ${int(r.calls_24h ?? 0)} in 24h</span>`
      : `<span class="cat">no calls</span>`}</td>`;
    const xh = r.x_heat;
    const x = xh ? `<td class="dz-c-x" data-tipid="${trackTip(`<b>X · ${esc(sym)}</b><div class="tip-text">Mood ${xh.mood > 0 ? "+" : ""}${xh.mood ?? "–"} (${esc(moodWord((xh.mood ?? 0) / 5) || "no read")}), ${xh.posts_h ?? "–"} posts an hour: ${esc(xh.label || "")}.</div>`)}">
        <span class="dz-x">${moodDab((xh.mood ?? 0) / 5)}<span><b>${xh.posts_h ?? "–"}</b>/h</span></span><span class="dz-sub dz-heat h-${esc(xh.label || "none")}">${esc(xh.label || "")}</span></td>`
      : `<td class="dz-c-x"><span class="cat" title="${F.mode === "fallback" ? "X heat comes with the evidence engine" : "No X read for this token yet"}">–</span></td>`;
    const ev = r.score ? `<td class="dz-c-ev" data-tipid="${trackTip(`<b>Evidence · ${esc(sym)}</b><div class="tip-text">${scored(r.score) ? `Score ${Math.round(r.score.value)} of 100 from` : "No score yet (too few lines known):"} ${tallyWords(byRule(r.score, r.red_flags?.length))}. A tally of the checklist, not a prediction.</div>`)}">${tally(r.score, r.red_flags?.length)}</td>`
      : `<td class="dz-c-ev"><span class="cat" title="${F.mode === "fallback" ? "Scored once the evidence engine is live" : "No checklist for this token yet"}">${F.mode === "fallback" ? "not scored yet" : "–"}</span></td>`;
    const fc = r.flag_checks;
    const flags = r.red_flags == null ? `<td class="dz-c-flags"><span class="cat">not checked yet</span></td>`
      : !r.red_flags.length ? `<td class="dz-c-flags">${flagCoverage(fc)}</td>`
      : `<td class="dz-c-flags"><span class="dz-flags">${r.red_flags.slice(0, 2).map((f) => `<span class="dz-flag">${esc(f)}</span>`).join("")}${
        r.red_flags.length > 2 ? `<span class="dz-flag more" title="${esc(r.red_flags.slice(2).join(" · "))}">+${r.red_flags.length - 2}</span>` : ""}</span></td>`;
    const capc = `<td class="dz-c-cap">${usd(r.mcap)}<span class="dz-sub">liq ${usd(r.liquidity)}</span></td>`;
    return `<tr class="dz-row" data-key="${esc(k)}">${tok}${px}${since}<td class="dz-c-spark">${spark(r)}</td>${calls}${x}${ev}${flags}${capc}</tr>`;
  }

  // ─── feed wiring ─────────────────────────────────────────────────────────
  retries.set("deep", () => { ENGINE.downAt = 0; loadFeed(); });
  // After a view or chain change the engine's rows are the wrong rows, not stale ones: show loading until the new ones land.
  const dropRows = () => { if (F.mode === "deep") Object.assign(F, { data: null, mode: null }); };
  $("#dz-view").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b || b.dataset.view === F.view) return;
    Object.assign(F, { view: b.dataset.view, sort: null, shown: PAGE });
    dropRows();
    renderFeed(); loadFeed();
  });
  $("#dz-chain").addEventListener("change", (e) => {
    Object.assign(F, { chain: e.target.value, shown: PAGE });
    dropRows();
    renderFeed(); loadFeed();
  });
  $("#dz-clean").addEventListener("change", (e) => { F.clean = e.target.checked; F.shown = PAGE; renderFeed(); });

  // ─── search (2026-10-06): a ticker, a name or a contract address ──────────────────
  // Tokens on our radar match as you type (GET /api/deep/search: called, priced or discovered); a ticker that several
  // tokens share lists them all, with their chain, so you pick one. A pasted address we don't have can be looked up
  // and put on the watch list (POST /api/deep/watch), which composes its dossier.
  const SR = { seq: 0, busy: false, note: null };
  const flagWords = (r) => {
    if (!r) return "";
    if (r.red_flags?.length) return r.red_flags.length === 1 ? "1 red flag" : `${r.red_flags.length} red flags`;
    const fc = r.flag_checks;
    return !fc || !fc.ran ? "not checked" : fc.ran < fc.of ? `${fc.ran} of ${fc.of} checked` : "no red flag";
  };
  function hitHtml(t) {
    const facts = [CHAIN_NAME[t.chain] || t.chain, t.age_s != null ? `${dur(t.age_s)} old` : "", t.mcap ? `mcap ${usd(t.mcap)}` : "", flagWords(t.row)].filter(Boolean);
    return `<button type="button" class="dz-hit" data-open="${esc(t.chain)}/${esc(t.address)}" role="option"><b>${esc(t.symbol || short(t.address))}</b>
      <span>${esc(t.name && t.name !== t.symbol ? `${t.name} · ` : "")}${esc(facts.join(" · "))}</span></button>`;
  }
  async function runSearch() {
    const box = $("#dz-sr"), q = $("#dz-q").value.trim(), seq = ++SR.seq;
    if (!q) { box.hidden = true; box.innerHTML = ""; box.classList.remove("busy"); SR.note = null; return; }
    // a search takes ~0.4 s, over a second while the feed's caches rebuild: say so at once, or keep the last answer
    // (dimmed) up while the next one comes
    if (box.hidden || !box.innerHTML) { box.innerHTML = `<p class="dz-sr-h">Searching…</p>`; box.hidden = false; }
    box.classList.add("busy");
    let d;
    try { d = await get(`/api/deep/search?q=${encodeURIComponent(q)}`); }
    catch (e) { if (seq === SR.seq) { box.innerHTML = `<p class="dz-sr-h">Couldn't search: ${esc(whyFailed(e))}.</p>`; box.hidden = false; box.classList.remove("busy"); } return; }
    if (seq !== SR.seq) return;                  // a newer keystroke is already searching
    box.classList.remove("busy");
    const hits = d.hits.map((h) => ({ ...h, row: h }));
    const same = hits.filter((t) => (t.symbol || "").toLowerCase() === (d.q || q).toLowerCase());     // d.q: without a "$"
    let html;
    if (hits.length) {
      html = (same.length > 1 ? `<p class="dz-sr-h">${same.length} tokens share the ticker ${esc(same[0].symbol)}. Pick one; pasting the contract address avoids this.</p>` : "")
        + hits.map(hitHtml).join("");
    } else if (d.address) {
      html = `<div class="dz-sr-none"><p><b>Not on our radar yet.</b> No group has called this address and discovery hasn't found it.</p>
        <button type="button" class="dz-run" data-run="${esc(q)}"${SR.busy ? " disabled" : ""}>${SR.busy ? "Looking it up…" : "Run the checks"}</button>
        ${SR.note ? `<p class="dz-sr-err">${esc(SR.note)}</p>` : ""}</div>`;
    } else {
      html = `<p class="dz-sr-h">No token on our radar matches "${esc(q)}". Paste its contract address to look it up.</p>`;
    }
    box.innerHTML = html;
    box.hidden = false;
  }
  let srTimer;
  document.addEventListener("click", (e) => { if (!e.target.closest(".dz-search")) $("#dz-sr").hidden = true; });
  $("#dz-q").addEventListener("focus", () => { if ($("#dz-q").value.trim()) runSearch().catch(console.error); });
  $("#dz-q").addEventListener("input", () => { SR.note = null; clearTimeout(srTimer); srTimer = setTimeout(() => runSearch().catch(console.error), 180); });
  $("#dz-q").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.target.value = ""; runSearch(); }
    if (e.key === "Enter") { const first = $("#dz-sr .dz-hit"); if (first && $("#dz-sr").querySelectorAll(".dz-hit").length === 1) first.click(); }
  });
  $("#dz-sr").addEventListener("click", async (e) => {
    const hit = e.target.closest(".dz-hit[data-open]");
    if (hit) {
      const [chain, ...rest] = hit.dataset.open.split("/");
      $("#dz-q").value = ""; $("#dz-sr").hidden = true;
      location.hash = `#/deep/${encodeURIComponent(chain)}/${encodeURIComponent(rest.join("/"))}`;
      return;
    }
    const run = e.target.closest(".dz-run[data-run]");
    if (!run || SR.busy) return;
    SR.busy = true; SR.note = null; runSearch();
    try {
      const r = await fetch("/api/deep/watch", { method: "POST", headers: { "Content-Type": "application/json", "X-Albatross-Action": "1" },
        body: JSON.stringify({ address: run.dataset.run }) });
      if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); return; }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `the server answered ${r.status}`);
      $("#dz-q").value = ""; $("#dz-sr").hidden = true;
      location.hash = `#/deep/${encodeURIComponent(j.chain)}/${encodeURIComponent(j.address)}`;
    } catch (err) {
      SR.note = err.message;
    } finally {
      SR.busy = false;
      if ($("#dz-q").value) runSearch();
    }
  });
  $("#dz-foot").addEventListener("click", (e) => { if (e.target.closest(".dz-more")) { F.shown += PAGE; renderFeed(); } });
  $("#dz-feed").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-k]");
    if (th) {
      const col = COLS.find((c) => c[0] === th.dataset.k);
      F.dir = F.sort === col[0] ? -F.dir : col[4] ? 1 : -1;
      F.sort = col[0];
      renderFeed();
      return;
    }
    if (e.target.closest(".dz-reset")) {
      Object.assign(F, { chain: "", clean: false, shown: PAGE });
      dropRows();
      renderFeed(); loadFeed();
      return;
    }
    const tr = e.target.closest("tr.dz-row");
    if (!tr || e.target.closest("a")) return;           // the token link opens it through the shared handler below
    const a = tr.querySelector("a[data-dossier]");
    openDossier(...splitKey(tr.dataset.key), "deep", a?.dataset.label);
  });

  // ═══ the dossier ═════════════════════════════════════════════════════════
  const TFS = ["1m", "5m", "15m", "1h"];
  const TF_MIN = { "1m": 1, "5m": 5, "15m": 15, "1h": 60 };
  const ASK = { "1m": 720, "5m": 2880, "15m": 10080, "1h": 20160 };      // minutes of history asked for, per timeframe
  const MICRO_ASK = { "1m": 720, "5m": 1800, "15m": 5400, "1h": 7200 };  // the fallback: its buckets divide each timeframe
  const D = { key: null, data: null, mode: null, why: null, at: 0, seq: 0, error: null, micro: null,
    tf: null, tfPref: null, log: null, candles: null, candlesAt: 0, cseq: 0, fitKey: null, open: new Map(), chart: null };
  try { const t = localStorage.getItem("albatross.deep.tf"); if (TFS.includes(t)) D.tfPref = t; } catch { /* no storage: defaults by age */ }
  // Until the reader picks one, the timeframe fits the token's life: a new coin in minutes, an older one in hours.
  const tfFor = (age) => (age == null || age < 2 * 3600 ? "1m" : age < 12 * 3600 ? "5m" : age < 3 * 86400 ? "15m" : "1h");
  const splitKey = (k) => { const [c, ...a] = String(k).split("/"); return [c, a.join("/")]; };

  // Wide screens: the evidence and the checks (security, fit, trades) on the left; the chart and what's happening (holders,
  // X, groups, news) on the right. Narrower: one column, header → evidence → chart → sections (deep.css orders them).
  dossTab.innerHTML = `
    <div class="dz-msg" id="dz-msg" hidden></div>
    <div class="dz-page" id="dz-page" hidden>
      <section class="panel dz-head" id="dz-head" aria-label="Token"></section>
      <div class="dz-grid">
       <div class="dz-col">
        <article class="panel dz-ev" id="dz-ev" aria-label="Evidence"></article>
        <div class="dz-secs" id="dz-secs-l"></div>
       </div>
       <div class="dz-col">
        <article class="panel dz-chartp" aria-label="Chart">
          <header class="dz-chart-h"><h2>Chart</h2>
            <div class="chips small dz-tf" id="dz-tf" role="group" aria-label="Timeframe">${TFS.map((t) => `<button type="button" data-tf="${t}">${t}</button>`).join("")}</div>
            <div class="chips small"><button type="button" class="dz-log" id="dz-log" aria-pressed="false" title="Logarithmic price scale: equal moves in percent look equal">Log</button></div>
          </header>
          <div class="dz-mk-kinds" id="dz-mk-kinds" role="group" aria-label="Events on the chart"></div>
          <div class="dz-legend" id="dz-legend"></div>
          <div class="dz-chart-wrap" id="dz-chart-wrap"><div class="dz-chart" id="dz-chart"></div><div class="dz-chart-alt" id="dz-chart-alt" hidden></div>
            <div class="dz-marks" id="dz-marks"></div><div class="dz-mk-card" id="dz-mk-card" role="dialog" aria-label="Event" hidden></div>
            <div class="dz-chart-msg" id="dz-chart-msg" hidden></div></div>
          <p class="dz-chart-note" id="dz-chart-note"></p>
          <div class="dz-stats" id="dz-stats"></div>
        </article>
        <div class="dz-secs" id="dz-secs-r"></div>
       </div>
      </div>
    </div>`;

  function openDossier(chain, address, from, label) {
    if (!chain || !address) return;
    if (state.tab === "deep") F.scroll = scrollY;
    const same = state.deep?.chain === chain && state.deep?.address === address;
    if (!same) state.deep = { chain, address, label: label || null };
    state.deepFrom = from || (state.tab === "micro" ? "micro" : state.tab === "dossier" ? state.deepFrom : "deep");
    setTab("dossier");
  }
  window.deepOpen = openDossier;        // micro.js (the token list and its drawer) opens dossiers through this

  // Any link to a dossier (the feed, the token list, the micro drawer, a copycat) opens it here; the href serves new tabs.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-dossier]");
    if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const [chain, address] = splitKey(a.dataset.dossier);
    const from = a.closest("#tab-micro, #drawer") ? "micro" : a.closest("#tab-deep") ? "deep" : undefined;
    if ($("#drawer").classList.contains("open")) $("#drawer-close").click();
    openDossier(chain, address, from, a.dataset.label);
  });

  function msg(html, kind) {
    const el = $("#dz-msg");
    el.hidden = !html;
    el.className = `dz-msg${kind ? ` ${kind}` : ""}`;
    el.innerHTML = html || "";
    $("#dz-page").hidden = !D.data;
  }

  function showDossier() {
    const k = `${state.deep.chain}/${state.deep.address}`;
    if (D.key !== k) {
      Object.assign(D, { key: k, data: null, mode: null, why: null, error: null, candles: null, candlesAt: 0, log: null, fitKey: null, micro: null, tf: D.tfPref });
      clearChart();
      const who = state.deep.label ? esc(state.deep.label) : `<span class="mono">${esc(short(state.deep.address))}</span>`;
      msg(`<div class="load-state loading" role="status"><span class="msg">Diving for the evidence on ${who}…</span></div>`);
    }
    syncChartControls();
    loadDossier();
  }

  // The dossier in the contract's shape (§4), built from the price pipeline's token page while the evidence engine
  // isn't answering: what it knows (price, pools, calls, bot trades) and nulls for the rest, which the page calls unknown.
  function fromMicro(m) {
    const t = m.token, px = t.price_usd ?? t.last_price_usd;
    const since = (p) => (p && px ? (px / p - 1) * 100 : null);
    const calls = [...(m.calls || [])].reverse().map((c) => ({ at: c.called_at, group: c.group_name, caller: c.caller, intent: c.intent,
      price: c.price, since_pct: since(c.price) }));
    const groups = new Map();
    for (const c of [...calls].reverse()) {
      const g = groups.get(c.group) || { name: c.group, n: 0, first_at: c.at, caller_stats: null };
      g.n += 1;
      groups.set(c.group, g);
    }
    const ds = DEXSCREENER[t.chain];
    return {
      fallback: true,
      token: { chain: t.chain, address: t.address, symbol: t.symbol, name: t.name, logo: null, launchpad: t.launchpad,
        created_at: t.created_at, age_s: t.age_s, description: null, same_name: null, copycat_of: null,
        links: { dexscreener: t.pool && ds ? `https://dexscreener.com/${ds}/${t.pool}` : null,
          birdeye: ["solana", "bsc", "ethereum", "base"].includes(t.chain) ? `https://birdeye.so/token/${t.address}?chain=${t.chain}` : null,
          explorer: EXPLORER[t.chain] ? `${EXPLORER[t.chain][1]}/token/${t.address}` : null,
          pumpfun: /pump/i.test(t.launchpad || "") && t.chain === "solana" ? `https://pump.fun/coin/${t.address}` : null } },
      market: { price: px, price_ts: t.price_ts, stale: !!t.stale && px != null, mcap: t.mcap_usd, fdv: t.fdv_usd, liquidity: t.liquidity_usd,
        change: null, volume: { h24: t.volume_h24_usd }, txns_h1_total: t.txns_h1, as_of: t.price_ts },
      call: t.call_at ? { first_at: t.call_at, first_group: t.call_group, first_price: t.call_price, since_pct: since(t.call_price),
        peak_x: t.peak_x, peak_at: t.peak_at, n_calls: t.n_calls, n_groups: t.n_groups, calls_24h: t.n_calls_24h, groups: [...groups.values()] } : null,
      calls, bot: m.trades || [], security: null, holders: null, traders: null, x: null, fit: null, news: null, checklist: [], score: null, freshness: {},
    };
  }

  async function loadDossier() {
    if (state.tab !== "dossier" || !state.deep) return;
    const { chain, address } = state.deep, k = `${chain}/${address}`, seq = ++D.seq;
    let d = null, mode = "deep", why = null;
    if (engineUp()) {
      try {
        d = await get(`/api/deep/token/${enc(chain)}/${enc(address)}`);
        ENGINE.downAt = 0;
      } catch (e) {
        why = e;
        engineFailed(e);            // a 404 is an answer about this token, not an outage
      }
    } else why = ENGINE.why;
    if (!d) {
      try {
        const m = await get(`/api/micro/token/${enc(chain)}/${enc(address)}?minutes=${MICRO_ASK["1m"]}`);
        d = fromMicro(m); mode = "fallback";
        if (seq === D.seq) D.micro = { at: Date.now(), raw: m };
      } catch (e) {
        if (seq !== D.seq || D.key !== k) return;
        if (e.status === 404) return missing(why);
        D.error = whyFailed(e);
        if (!D.data) msg(`<div class="load-state failed"><b>Couldn't open this dossier.</b> <span class="msg">${esc(cap(D.error))}. Retrying automatically.</span>
          <button class="load-retry" data-retry="dossier">Try now</button></div>`);
        else banner();
        return;
      }
    }
    if (seq !== D.seq || D.key !== k) return;
    const modeChanged = D.mode && D.mode !== mode;
    Object.assign(D, { data: d, mode, why, at: Date.now(), error: null });
    renderDossier();
    if (modeChanged || !D.candles || Date.now() - D.candlesAt > 15_000) loadCandles();
  }

  function missing(why) {
    D.data = null;
    msg(`<article class="panel dz-missing"><img src="/static/art/albatross-cutout.webp" alt="" loading="lazy"><div>
      <h2>Not a token we watch</h2><p>Neither the evidence engine${why && why.status !== 404 ? ` (${esc(why.status === 503 ? "not live yet" : whyFailed(why))})` : ""} nor the price pipeline knows
      <span class="mono">${esc(state.deep.address)}</span> on ${esc(CHAIN_NAME[state.deep.chain] || state.deep.chain)}. A token joins the Deep End when a group calls it,
      the bot trades it, or it trends.</p><a class="btn-paint" href="#/deep" data-go="deep">Back to the feed</a></div></article>`);
  }

  function banner() {
    const parts = [];
    if (D.mode === "fallback") {
      const w = D.why ? (D.why.status === 503 ? "it answered “being built” (HTTP 503)" : D.why.status === 404 ? "it isn't watching this token yet" : whyFailed(D.why)) : "";
      parts.push(`<div class="dz-banner"><span><b>The evidence engine isn't live for this token</b>${w ? `: ${esc(w)}` : ""}. This dossier shows what the price
        pipeline knows (price, pools, calls, bot trades, the chart); the evidence tally, security, holders, X and fit appear when it answers.</span></div>`);
    }
    if (D.error) parts.push(`<div class="load-note"><span><b>Couldn't refresh this dossier:</b> ${esc(D.error)}. Showing what loaded ${esc(ago(D.at))}; retrying automatically.</span>
      <button class="load-retry" data-retry="dossier">Try now</button></div>`);
    msg(parts.join(""));
  }

  // ─── the dossier, rendered ───────────────────────────────────────────────
  const ICON = {
    x: `<path d="M5 4l14 16M19 4 5 20"/>`,
    telegram: `<path d="M21 4 3 11.5l6.5 2.2L12 20l3.2-4.2L20 19z"/><path d="m9.5 13.7 6-4.6"/>`,
    website: `<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 2.6 14.6 0 17M12 3.5c-2.6 2.4-2.6 14.6 0 17"/>`,
    dexscreener: `<path d="M6 4v16M18 4v16"/><rect x="4" y="8" width="4" height="7" rx="1"/><rect x="16" y="6" width="4" height="8" rx="1"/><path d="M12 3v18"/><rect x="10" y="10" width="4" height="6" rx="1"/>`,
    birdeye: `<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>`,
    explorer: `<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>`,
    pumpfun: `<rect x="3.5" y="8.5" width="17" height="7" rx="3.5" transform="rotate(-35 12 12)"/><path d="m10.2 7.2 3.6 9.6" transform="rotate(-35 12 12)"/>`,
  };
  const icon = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[k] || ICON.website}</svg>`;

  function headHtml(d) {
    const t = d.token, m = d.market || {}, links = t.links || {}, sym = t.symbol || short(t.address);
    const nm = t.name && t.name.trim().toLowerCase() !== (t.symbol || "").trim().toLowerCase() ? t.name : "";
    const age = t.age_s != null ? `<span title="${t.created_at ? `Created ${esc(utc(t.created_at))}` : "Since its first pool"}">${dur(t.age_s)} old</span>` : "";
    const paid = m.dex_paid ? `<span class="dz-tag${m.dex_paid.paid ? " ok" : ""}" title="${m.dex_paid.paid ? `DexScreener profile paid${m.dex_paid.at ? ` ${esc(ago(m.dex_paid.at))}` : ""}` : "No paid DexScreener profile"}">${m.dex_paid.paid ? "Dex paid" : "Dex not paid"}</span>` : "";
    const boosts = m.boosts ? `<span class="dz-tag" title="Active DexScreener boosts">${plural(m.boosts, "boost")}</span>` : "";
    const exName = EXPLORER[t.chain]?.[0] || "Explorer";
    const L = [["x", "X"], ["telegram", "Telegram"], ["website", "Website"], ["dexscreener", "DexScreener"], ["birdeye", "Birdeye"], ["explorer", exName], ["pumpfun", "pump.fun"]]
      .map(([k, label]) => [k, label, safeUrl(links[k])]).filter(([, , u]) => u);
    const chg = m.change ? ["m5", "h1", "h6", "h24"].filter((p) => m.change[p] != null)
      .map((p) => `<span class="dz-chg">${delta(m.change[p], 1)} <span class="dz-per">${p.slice(1)}${p[0] === "m" ? "m" : "h"}</span></span>`).join("") : "";
    const cc = t.copycat_of;
    const ccName = cc ? esc(cc.symbol || cc.name || short(cc.address)) : "";
    const ccFacts = cc ? [cc.chain && cc.chain !== t.chain ? `on ${esc(CHAIN_NAME[cc.chain] || cc.chain)}` : "", cc.created_at ? `created ${esc(ago(cc.created_at))}` : "",
      cc.liquidity != null ? `${usd(cc.liquidity)} liquidity` : ""].filter(Boolean).join(", ") : "";
    const copy = cc ? `<p class="dz-warn"><b>Possible copycat.</b> ${cc.address || cc.symbol ? `Another contract with this name looks like the original: ${cc.chain && cc.address
        ? `<a href="#/deep/${esc(cc.chain)}/${esc(cc.address)}" data-dossier="${esc(cc.chain)}/${esc(cc.address)}" data-label="${ccName}">${ccName}</a>` : ccName}${ccFacts ? ` (${ccFacts})` : ""}`
        : "Another contract has the better claim to this name"}${cc.reason ? `: ${esc(cc.reason)}` : ""}. Check the address before you trade.</p>`
      : t.same_name ? `<p class="dz-note">${plural(t.same_name, "other contract")} use${t.same_name === 1 ? "s" : ""} the name ${esc(sym)}. Check the address before you trade.</p>` : "";
    return `<div class="dz-id">${logo(t, "big")}
        <div class="dz-id-t"><h2><span class="dz-sym">${esc(sym)}</span>${nm ? ` <span class="dz-nm">${esc(nm)}</span>` : ""}</h2>
          <div class="dz-meta">${chainChip(t.chain)} <span>${esc(CHAIN_NAME[t.chain] || t.chain)}</span>${t.launchpad ? `<span>${esc(t.launchpad)}</span>` : ""}${age}${paid}${boosts}</div></div>
        <div class="dz-pxb">${m.price != null ? `<div class="dz-bigpx${m.stale ? " mc-dim" : ""}">$${priceHtml(m.price)}${m.stale ? ` <span class="mc-stale">stale</span>` : ""}</div>`
          : `<div class="dz-bigpx none" title="A price older than its limit is unknown, never shown as current">no fresh price</div>`}
          ${chg ? `<div class="dz-chgs">${chg}</div>` : `<div class="dz-chgs cat">${m.price_ts ? `priced ${esc(ago(m.price_ts))}` : "no price yet"}</div>`}</div></div>
      ${t.description ? `<p class="dz-desc">${esc(t.description)}</p>` : ""}
      <div class="dz-ca"><span class="dz-ca-k">CA</span><code class="dz-addr" title="${esc(t.address)}"><span class="full">${esc(t.address)}</span><span class="short">${esc(short(t.address))}</span></code>
        <button type="button" class="dz-copy" data-copy="${esc(t.address)}">Copy CA</button></div>
      ${L.length ? `<nav class="dz-links" aria-label="Links">${L.map(([k, label, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${icon(k)}${esc(label)}<span class="ext" aria-hidden="true">↗</span></a>`).join("")}</nav>` : ""}
      ${copy}`;
  }

  // ─── evidence card: a dial for the tally, the checklist (worst first), the Albatross take ─────
  const STATUS = { good: ["✓", "good"], warn: ["!", "warn"], bad: ["✕", "bad"], unknown: ["?", "unknown"] };
  // dossier.RED_FLAG_KEYS, for dossiers composed before each line said whether it is a red-flag check
  const FLAG_KEYS = new Set(["mint", "lp", "dev", "insiders", "wallets", "top10", "tax", "liquidity", "organic", "reddit", "github"]);
  const isFlagCheck = (c) => c.flag_check ?? FLAG_KEYS.has(c.key);
  const RANK = { bad: 0, warn: 1, good: 2, unknown: 3 };
  // Which section holds the detail behind a checklist line (tap a line to go there).
  const SEC_OF = { mint: "security", freeze: "security", lp: "security", dev: "security", honeypot: "security", tax: "security",
    rugcheck: "security", transfer_fee: "security", insiders: "holders", top10: "holders", holders: "holders", clusters: "holders",
    organic: "trades", flow: "trades", smart: "trades", traders: "trades", liquidity: "market", dexpaid: "market", volume: "market",
    boosts: "market", calls: "groups", callers: "groups", x: "x", kols: "x", account: "x", fit: "fit", copycat: "fit", news: "news",
    age: "market", github: "github", fomo: "fomo", reddit: "reddit", wallets: "wallets" };

  // The arc is SVG; its number and ends are HTML laid over it, so they are text a reader (and the readability gate) sees.
  function dial(v) {
    const ok = v != null && isFinite(v), f = ok ? Math.max(0, Math.min(100, v)) : 0;
    return `<div class="dz-dial${ok ? "" : " none"}" role="img" aria-label="${ok ? `Evidence score ${Math.round(v)} out of 100` : "No evidence score yet"}">
      <svg viewBox="0 0 160 88" aria-hidden="true"><path class="trk" d="M16 82a64 64 0 0 1 128 0" pathLength="100"/>
      ${f > 0 ? `<path class="val" d="M16 82a64 64 0 0 1 128 0" pathLength="100" stroke-dasharray="${f.toFixed(1)} 100"/>` : ""}</svg>
      <b class="num" aria-hidden="true">${ok ? Math.round(v) : "–"}</b><span class="end lo" aria-hidden="true">0</span><span class="end hi" aria-hidden="true">100</span></div>`;
  }

  function evidenceHtml(d) {
    const sc = d.score, cl = [...(d.checklist || [])].sort((a, b) => (RANK[a.status] ?? 3) - (RANK[b.status] ?? 3));
    if (!sc && !cl.length) {
      return `<header class="panel-h"><h2>Evidence</h2></header>
        <div class="dz-ev-empty"><img class="dz-bird" src="/static/art/albatross-cutout.webp" alt="" loading="lazy">
          <p class="dz-empty-t">${d.fallback ? "The evidence engine is still being built." : "No evidence tally for this token yet."}</p>
          <p>${d.fallback ? "Once it answers, this card tallies security, holders, flow, the calls, X and culture fit into a checklist, with the Albatross take. The price, the calls and the chart are below already."
            : "The dossier composer tallies hot and warm tokens every minute; this one hasn't been composed yet."}</p></div>`;
    }
    // Lines from sources not yet graded (GitHub, FOMO, Reddit, wallets) show under their own heading and count for
    // nothing in the score (dossier.UNSCORED).
    const inScore = cl.filter((c) => c.scored !== false), extra = cl.filter((c) => c.scored === false);
    const n = inScore.length || (sc.good || 0) + (sc.warn || 0) + (sc.bad || 0) + (sc.unknown || 0);
    // One rule everywhere: a red flag is a failed red-flag check; any other failure or warning is a caution.
    const red = cl.filter((c) => c.status === "bad" && isFlagCheck(c));
    const groups = [
      ["bad", "✕", red.length, red.length === 1 ? "red flag" : "red flags"],
      ["warn", "!", inScore.filter((c) => c.status === "warn" || (c.status === "bad" && !isFlagCheck(c))).length, "cautions"],
      ["unknown", "?", inScore.filter((c) => c.status === "unknown").length, "unknown"],
      ["ctx", "•", extra.filter((c) => !(c.status === "bad" && isFlagCheck(c))).length, "context only"],
      ["good", "✓", inScore.filter((c) => c.status === "good").length, "passed"],
    ];
    const counts = cl.length ? groups.map(([k, g, v, w]) => `<span class="dz-cnt c-${k}"><i>${g}</i><b>${v}</b> ${w}</span>`).join("") : "";
    // The red-flag line leads: what failed, or how much of the red-flag check list actually ran.
    const fc = d.flag_checks, labelOf = Object.fromEntries(cl.map((c) => [c.key, c.label || c.key]));
    const flagLine = red.length ? `<p class="dz-fl bad"><b>${red.length === 1 ? "1 red flag" : `${red.length} red flags`}</b> · ${red.map((c) => esc(c.label || c.key)).join(" · ")}</p>`
      : !fc ? ""
      : fc.ran >= fc.of ? `<p class="dz-fl ok"><b>No red flag</b> · all ${fc.of} red-flag checks ran</p>`
      : fc.ran ? `<p class="dz-fl part"><b>${fc.ran} of ${fc.of} red-flag checks ran, none failed yet</b> · not run yet: ${esc((fc.missing || []).map((k) => labelOf[k] || k).join(", "))}</p>`
      : `<p class="dz-fl none"><b>Not checked</b> · none of the ${fc.of} red-flag checks has run yet</p>`;
    const line = (c) => {
      // a failure that is not a red-flag check is a caution, drawn like one (the ✕ is for red flags only)
      const st = c.status === "bad" && !isFlagCheck(c) ? "warn" : STATUS[c.status] ? c.status : "unknown";
      const [g0, word0] = STATUS[st], target = SEC_OF[c.key];
      const word = st !== c.status ? "failed, a caution (not a red-flag check)" : word0;
      const g = c.scored === false ? "•" : g0;               // not in the score: no ✓/!/✕ verdict mark
      const src = [c.source ? esc(c.source) : "", c.as_of ? when(c.as_of) : ""].filter(Boolean).join(" · ");
      return `<li class="dz-ck s-${esc(c.scored === false ? "unscored" : st)}"${target ? ` data-sec="${target}" tabindex="0" title="Show the detail"` : ""}>
        <i class="dz-ic" role="img" aria-label="${c.scored === false ? `${word}, not scored` : word}">${g}</i>
        <div class="dz-ck-b"><div class="dz-ck-top"><span class="dz-ck-l">${esc(c.label || c.key)}</span><span class="dz-ck-v">${c.value == null ? `<span class="cat">${c.status === "unknown" ? "not known yet" : "–"}</span>` : esc(c.value)}</span></div>
          ${c.detail ? `<div class="dz-ck-d">${esc(c.detail)}</div>` : ""}
          ${src ? `<div class="dz-ck-src">${src}</div>` : ""}</div></li>`;
    };
    // The engine's score: good lines and half the warns, over the lines it knows; unknowns count for nothing.
    const what = scored(sc) ? `${plural(n, "check")}: the score counts the good lines and half the warns against every line known. Not graded against outcomes yet.`
      : `${plural(n, "check")}, too few known for a score yet (it needs five).`;
    return `<header class="panel-h"><h2>Evidence</h2>${asOf(sc?.take_at || d.freshness?.market, "tallied")}</header>
      ${flagLine}
      ${sc ? `<div class="dz-ev-top">${dial(sc.value)}<div class="dz-ev-sum">
        <p class="dz-ev-what"><b>A tally of the evidence, not a prediction.</b> ${what}</p>
        <span class="dz-tbar big" aria-hidden="true">${["good", "warn", "bad", "unknown"].map((k) => (sc[k] ? `<i class="t-${k}" style="flex-grow:${sc[k]}"></i>` : "")).join("")}</span>
        <div class="dz-cnts big">${counts}</div></div></div>` : ""}
      ${inScore.length ? `<ul class="dz-cks">${inScore.map(line).join("")}</ul>` : ""}
      ${extra.length ? `<h3 class="dz-h3 dz-ck-new">Context only <span class="cat">newer sources, shown but not in the score until we've graded them</span></h3>
        <ul class="dz-cks dz-cks-new">${extra.map(line).join("")}</ul>` : ""}
      ${sc?.take ? `<figure class="dz-take"><figcaption><img src="/static/brand/logo-128.png" alt="" width="26" height="26"> The Albatross take ${asOf(sc.take_at, "·")}</figcaption>
        <blockquote>${esc(sc.take)}</blockquote></figure>` : ""}
      <p class="dz-ev-foot">Every line names its source and age. Missing data counts as unknown, never good.</p>`;
  }

  // ─── market numbers under the chart ──────────────────────────────────────
  function statsHtml(d) {
    const m = d.market || {}, v = m.volume || {};
    const tile = (k, val, sub) => `<div class="dz-stat"><div class="k">${k}</div><div class="v">${val}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
    const tiles = [
      tile("Market cap", usd(m.mcap), m.fdv != null && m.fdv !== m.mcap ? `FDV ${usd(m.fdv)}` : m.mcap == null ? "not reported" : ""),
      tile("Liquidity", usd(m.liquidity), m.liquidity && m.mcap ? `${pct((m.liquidity / m.mcap) * 100, 0)} of market cap` : ""),
      tile("Volume 24h", usd(v.h24), [v.h1 != null ? `1h ${usd(v.h1)}` : "", v.m5 != null ? `5m ${usd(v.m5)}` : ""].filter(Boolean).join(" · ")),
    ];
    if (!d.fallback || m.holders != null) tiles.push(tile("Holders", int(m.holders), m.holders_change_h1_pct != null ? delta(m.holders_change_h1_pct, 1, "1h") : ""));
    if (m.txns?.h1) tiles.push(tile("Buys / sells 1h", `${int(m.txns.h1.buys)} <small>/ ${int(m.txns.h1.sells)}</small>`,
      m.net_buyers_h1 != null ? `${int(m.net_buyers_h1)} net buyers` : ""));
    else if (m.txns_h1_total != null) tiles.push(tile("Trades 1h", int(m.txns_h1_total), "in the pricing pool"));
    if (m.traders) tiles.push(tile("Traders 1h", int(m.traders.h1), m.traders.h24 != null ? `24h ${int(m.traders.h24)}` : ""));
    if (m.organic_score != null) tiles.push(tile("Organic volume", `${Math.round(m.organic_score)}<small>/100</small>`, esc(m.organic_label || "")));
    if (!d.fallback) tiles.push(tile("Dex paid", m.dex_paid ? (m.dex_paid.paid ? "Paid" : "No") : "–",
      m.dex_paid?.paid && m.dex_paid.at ? esc(ago(m.dex_paid.at)) : m.boosts ? plural(m.boosts, "boost") : ""));
    return `${tiles.join("")}${m.as_of ? `<p class="dz-stats-foot">Market numbers ${asOf(m.as_of)}</p>` : ""}`;
  }

  // ─── the sections ────────────────────────────────────────────────────────
  // Each is a fold: open on wide screens, closed on phones until tapped, and it stays how the reader left it.
  function sec(k, title, gist, ts, body) {
    const open = D.open.has(k) ? D.open.get(k) : !PHONE.matches;
    return `<details class="panel dz-sec" data-k="${k}" id="dz-sec-${k}"${open ? " open" : ""}>
      <summary><span class="dz-sec-h"><h2>${title}</h2>${ts ? asOf(ts) : ""}</span>${gist ? `<span class="dz-gist">${gist}</span>` : ""}</summary>
      <div class="dz-sec-b">${body}</div></details>`;
  }
  const empty = (b, rest = "") => `<div class="dz-empty"><b>${b}</b>${rest ? ` ${rest}` : ""}</div>`;
  const notYet = (d, what) => (d.fallback ? empty(`No ${what} yet.`, "It comes with the evidence engine, which isn't live for this token yet.")
    : empty(`No ${what} for this token yet.`, "Hot tokens are read first; this one hasn't been reached."));
  const kv = (items) => `<div class="dz-kv">${items.filter(Boolean).map(([k, v, s]) => `<div><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`).join("")}</div>`;
  const tagChip = (t) => `<span class="dz-htag t-${esc(String(t).toLowerCase())}">${esc(t)}</span>`;
  const wallet = (chain, a) => {
    const u = walletUrl(chain, a);
    return u ? `<a class="mono dz-wallet" href="${esc(u)}" target="_blank" rel="noopener noreferrer" title="${esc(a)}">${esc(short(a))}</a>` : `<span class="mono" title="${esc(a)}">${esc(short(a))}</span>`;
  };
  // A small line for a series over time (holder count, posts an hour) that stretches to its column: the line keeps its
  // stroke, the last point is an HTML dot (a stretched SVG circle would turn oval), first and last are named underneath.
  function trend(pts, fmt, label) {
    const p = pts.filter((q) => q.v != null && q.t).sort((a, b) => a.t - b.t);
    if (p.length < 2) return "";
    const W = 300, H = 56, P = 5, lo = Math.min(...p.map((q) => q.v)), hi = Math.max(...p.map((q) => q.v)), span = hi - lo || 1;
    const t0 = p[0].t, t1 = p[p.length - 1].t;
    const x = (t) => P + ((t - t0) / (t1 - t0 || 1)) * (W - 2 * P), y = (v) => P + (1 - (v - lo) / span) * (H - 2 * P);
    const path = p.map((q, i) => `${i ? "L" : "M"}${x(q.t).toFixed(1)},${y(q.v).toFixed(1)}`).join("");
    const e = p[p.length - 1];
    return `<figure class="dz-trend"><div class="dz-trend-plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      <path d="${path}" vector-effect="non-scaling-stroke"/></svg><i style="left:${((x(e.t) / W) * 100).toFixed(2)}%;top:${((y(e.v) / H) * 100).toFixed(2)}%"></i></div>
      <figcaption>${label}: ${fmt(p[0].v)} at ${hhmm(p[0].t / 1000)} UTC → <b>${fmt(e.v)}</b> at ${hhmm(e.t / 1000)} UTC</figcaption></figure>`;
  }

  function holdersSec(d) {
    const h = d.holders, m = d.market || {};
    if (!h) return sec("holders", "Holders", m.holders != null ? `${int(m.holders)} holders` : "", null, notYet(d, "holder breakdown"));
    const top = h.top || [], listed = top.reduce((a, x) => a + (x.pct || 0), 0);
    const restTop = h.top10_pct != null ? Math.max(0, h.top10_pct - listed) : null, others = h.top10_pct != null ? Math.max(0, 100 - h.top10_pct) : null;
    const kind = (x) => { const t = (x.tags || []).map((s) => String(s).toLowerCase()); return t.includes("insider") || t.includes("dev") ? "risk" : t.includes("pool") || t.includes("lp") ? "pool" : t.includes("cex") ? "cex" : t.includes("smart") ? "smart" : "plain"; };
    const segs = top.map((x) => [kind(x), x.pct, `${short(x.owner)}: ${pct(x.pct)}${x.tags?.length ? ` (${x.tags.join(", ")})` : ""}`]);
    if (restTop > 0.05) segs.push(["rest", restTop, `The rest of the top 10: ${pct(restTop)}`]);
    if (others != null) segs.push(["others", others, `Everyone else: ${pct(others)}`]);
    const KIND = { risk: "Insider or dev", pool: "Pool or LP", cex: "Exchange", smart: "Smart money", plain: "Other top holders", rest: "Rest of the top 10", others: "Everyone else" };
    const present = [...new Set(segs.map((s) => s[0]))];
    const bar = segs.length ? `<div class="dz-dist" role="img" aria-label="Supply held: ${esc(segs.map((s) => s[2]).join("; "))}">${segs.map(([k, w, t]) =>
      `<i class="d-${k}" style="flex-grow:${Math.max(w, 0.3).toFixed(2)}" title="${esc(t)}"></i>`).join("")}</div>
      <div class="dz-legend-row">${present.map((k) => `<span><i class="d-${k}"></i>${KIND[k]}</span>`).join("")}</div>` : "";
    const rows = top.map((x, i) => `<tr><td class="cat">${i + 1}</td><td class="left">${wallet(d.token.chain, x.owner)}</td><td>${pct(x.pct)}</td>
      <td class="left">${(x.tags || []).map(tagChip).join(" ") || `<span class="cat">–</span>`}</td></tr>`).join("");
    const clusters = (h.clusters || []).map((c) => `<li><b>${esc(c.label)}</b><span>${plural(c.wallets, "wallet")} hold ${pct(c.pct)}</span>${c.reason ? `<p>${esc(c.reason)}</p>` : ""}</li>`).join("");
    const hist = trend((h.history || []).map((q) => ({ t: ms(q.ts), v: q.count })), int, "Holders");
    const gist = `${int(h.count)} ${h.change_h1_pct != null ? delta(h.change_h1_pct, 1, "1h") : ""} · top 10 hold ${pct(h.top10_pct)}${h.clusters?.length ? ` · ${plural(h.clusters.length, "cluster")}` : ""}`;
    return sec("holders", "Holders", gist, h.as_of || d.freshness?.holders, `
      ${kv([["Holders", int(h.count), h.change_h1_pct != null ? delta(h.change_h1_pct, 1, "1h") : ""], ["Top 10 hold", pct(h.top10_pct), "of the supply"],
        h.clusters?.length ? ["In clusters", pct(h.clusters.reduce((a, c) => a + (c.pct || 0), 0)), plural(h.clusters.reduce((a, c) => a + (c.wallets || 0), 0), "wallet")] : null])}
      ${bar}
      ${clusters ? `<h3 class="dz-h3">Clusters</h3><ul class="dz-clusters">${clusters}</ul>` : ""}
      ${rows ? `<h3 class="dz-h3">Top holders</h3><div class="table-wrap bare"><table class="plain dz-t"><thead><tr><th>#</th><th class="left">Wallet</th><th>Share</th><th class="left">Tags</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${hist}`);
  }

  function xSec(d) {
    const x = d.x;
    if (!x) return sec("x", "X", "", null, notYet(d, "X read"));
    const ask = xAskHtml(x);
    if (x.status === "none") return sec("x", "X", "", x.as_of, `${ask}${empty("No X read for this token yet.", x.below_floor
      ? `The X Scout's own reads skip tokens with under ${esc(usd(x.floor_usd))} of liquidity, and this one has less.`
      : "The X Scout reads hot tokens first, paced by the shared Grok budget.")}`);
    const budget = x.status === "budget", stale = x.status === "stale";
    if (budget && !x.summary && x.posts_h == null) return sec("x", "X", "", x.as_of, `${ask}${empty("No X read yet: today's X budget is spent.", "The X Scout picks up again when the budget resets.")}`);
    const note = budget ? `<p class="dz-flagline">Today's X budget is spent: this is the last read, ${esc(ago(x.as_of))}.</p>`
      : stale ? `<p class="dz-flagline">This read is ${esc(ago(x.as_of).replace(" ago", ""))} old; a fresh one hasn't landed yet.</p>` : "";
    const mood = x.mood != null ? `${moodDab(x.mood / 5)} <span class="dz-moodw">${esc(x.mood_label || moodWord(x.mood / 5))}</span>` : "–";
    const posts = (x.notable || []).map((p) => {
      const u = /^https:\/\/(x|twitter)\.com\//.test(p.url || "") ? p.url : null;
      const id = statusId(p.url);
      return `<li class="st-${esc(p.stance || "none")}"${id ? ` data-mkey="x:${esc(id)}"` : ""}><div class="xp-a">${esc(p.author)}${p.followers != null ? ` <span class="cat">${int(p.followers)} followers</span>` : ""}
        ${p.stance ? `<span class="dz-stance s-${esc(p.stance)}">${esc(p.stance)}</span>` : ""}</div><p>${esc(p.text)}</p>
        <div class="dz-postfoot">${p.at ? when(p.at) : ""}${u ? ` · <a href="${esc(u)}" target="_blank" rel="noopener noreferrer">Open on X ↗</a>` : ""}</div></li>`;
    }).join("");
    const handle = (h) => String(h || "").replace(/^@/, "");
    const kols = (x.kols || []).map((k) => /^@?\w{1,15}$/.test(k) ? `<a class="xs-coin" href="https://x.com/${esc(handle(k))}" target="_blank" rel="noopener noreferrer">@${esc(handle(k))}</a>` : `<span class="xs-coin">${esc(k)}</span>`).join(" ");
    const a = x.account;
    const acct = a ? kv([["Project account", /^@?\w{1,15}$/.test(a.handle || "") ? `<a href="https://x.com/${esc(handle(a.handle))}" target="_blank" rel="noopener noreferrer">@${esc(handle(a.handle))}</a>` : esc(a.handle || "–"),
      a.created ? `since ${esc(a.created)}` : ""], ["Followers", int(a.followers), ""], ["Name changes", int(a.name_changes), a.name_changes ? "a renamed account is a common rug tell" : "none seen"]]) : "";
    const hist = (x.history || []).length >= 2 ? trend(x.history.map((q) => ({ t: ms(q.ts), v: q.posts_h })), (v) => `${int(v)}/h`, "Posts an hour") : "";
    const gist = `${x.mood != null ? `${moodDab(x.mood / 5)} ` : ""}${esc(x.mood_label || "")}${x.posts_h != null ? ` · ${int(x.posts_h)} posts/h` : ""}${x.unique_authors != null ? ` · ${plural(x.unique_authors, "author")}` : ""}`;
    return sec("x", "X", gist, x.as_of || d.freshness?.x, `${ask}${note}
      ${kv([["Mood", mood, "−5 to +5"], ["Posts an hour", int(x.posts_h), ""], ["Authors", int(x.unique_authors), "posting in the read"]])}
      ${x.summary ? `<p class="dz-say">${esc(x.summary)}</p>` : ""}${x.why_it_matters ? `<p class="xs-why">${esc(x.why_it_matters)}</p>` : ""}
      ${posts ? `<h3 class="dz-h3">Notable posts</h3><ul class="xposts dz-posts">${posts}</ul>` : ""}
      ${kols ? `<h3 class="dz-h3">Accounts with reach posting it</h3><div class="dz-kols">${kols}</div>` : ""}
      ${acct ? `<h3 class="dz-h3">The token's own account</h3>${acct}` : ""}${hist}`);
  }

  function groupsSec(d) {
    const c = d.call, calls = d.calls || [];
    if (!c && !calls.length) return sec("groups", "Groups", "no calls", null, empty("No group has called it.", "It came onto the radar another way (trending, a new listing, or a bot trade)."));
    const first = c ? `<p class="dz-callline">First called <b>${when(c.first_at)}</b>${c.first_group ? ` in <b>${esc(c.first_group)}</b>` : ""}${c.first_price ? ` at <b>${price(c.first_price)}</b>` : ""}${
      c.since_pct != null ? `; now <span class="mc-since ${upDown(c.since_pct)}">${sinceFmt(c.since_pct)}</span>` : ""}${c.peak_x ? `. Highest since: <b>${xs(c.peak_x)}</b> the call price${c.peak_at ? ` (${when(c.peak_at)})` : ""}` : ""}.</p>` : "";
    const g = (c?.groups || []).map((x) => {
      const st = x.caller_stats;
      return `<tr><td class="left"><b>${esc(x.name)}</b></td><td>${int(x.n)}</td><td class="cat">${x.first_at ? when(x.first_at) : "–"}</td>
        <td class="left">${st ? `${plural(st.calls, "call")} graded · ${st.hit_rate_2x != null ? `${pct(st.hit_rate_2x * 100, 0)} reached 2×` : "–"} · median 24h <span class="mc-since ${upDown(st.median_24h_pct)}">${sinceFmt(st.median_24h_pct)}</span>`
          : `<span class="cat">not graded yet</span>`}</td></tr>`;
    }).join("");
    const rows = calls.slice(0, 60).map((x) => `<tr><td class="left cat">${when(x.at)}</td><td class="left">${esc(x.group || "–")}<span class="dz-sub">${esc(x.caller || "")}${
      x.intent && x.intent !== "mention" ? ` · ${esc(x.intent)} call` : ""}</span></td>
      <td>${x.price ? price(x.price) : "–"}</td><td>${x.since_pct == null ? "–" : `<span class="mc-since ${upDown(x.since_pct)}">${sinceFmt(x.since_pct)}</span>`}</td></tr>`).join("");
    const gist = c ? `${plural(c.n_calls ?? calls.length, "call")} from ${plural(c.n_groups ?? (c.groups || []).length, "group")} · first ${esc(ago(c.first_at))}` : plural(calls.length, "call");
    return sec("groups", "Groups", gist, D.at, `${first}
      ${g ? `<h3 class="dz-h3">Groups <span class="cat">their track record across every call we graded</span></h3><div class="table-wrap bare"><table class="plain dz-t"><thead><tr><th class="left">Group</th><th>Calls</th><th>First</th><th class="left">Track record</th></tr></thead><tbody>${g}</tbody></table></div>` : ""}
      ${rows ? `<h3 class="dz-h3">Calls <span class="cat">newest first${calls.length > 60 ? `, the 60 newest of ${calls.length}` : ""}</span></h3><div class="table-wrap bare"><table class="plain dz-t dz-calls"><thead><tr><th class="left">When</th><th class="left">Group · caller</th><th title="The price when the call was posted">Price then</th><th title="Price now against the price at this call">Since</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}`);
  }

  function fitSec(d) {
    const f = d.fit;
    if (!f || f.status === "none") return sec("fit", "Fit", "", f?.as_of, f ? empty("No culture-fit read yet.", "The fit researcher reads hot tokens first.") : notYet(d, "culture-fit read"));
    if (f.status === "budget" && !f.summary) return sec("fit", "Fit", "", f.as_of, empty("No fit read yet: today's research budget is spent.", ""));
    const note = f.status === "budget" ? `<p class="dz-flagline">Today's research budget is spent: this is the last read.</p>` : f.status === "stale" ? `<p class="dz-flagline">This read is ${esc(ago(f.as_of).replace(" ago", ""))} old.</p>` : "";
    const cites = (f.citations || []).map((c) => { const u = safeUrl(c.url); return u ? `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(c.title || u)} ↗</a></li>` : ""; }).join("");
    const dl = [["Meme", f.meme], ["Meta", f.meta], ["Audience", f.audience], ["Originality", f.originality]].filter(([, v]) => v);
    return sec("fit", "Fit", f.score != null ? `<b>${f.score}/10</b> ${esc(f.label || "")}` : esc(f.label || ""), f.as_of || d.freshness?.fit, `${note}
      ${f.score != null ? `<div class="dz-fit-score"><span class="dz-fit-n">${esc(f.score)}<small>/10</small></span><span class="dz-tag">${esc(f.label || "")}</span></div>` : ""}
      ${f.summary ? `<p class="dz-say">${esc(f.summary)}</p>` : ""}
      ${dl.length ? `<dl class="dz-dl">${dl.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : ""}
      ${(f.risks || []).length ? `<h3 class="dz-h3">Risks to the story</h3><div class="dz-flags">${f.risks.map((r) => `<span class="dz-flag soft">${esc(r)}</span>`).join("")}</div>` : ""}
      ${cites ? `<h3 class="dz-h3">Sources</h3><ul class="dz-cites">${cites}</ul>` : ""}`);
  }

  // The repo behind the token (albatross/deepend/github.py): its public metadata only, never its code. Most memecoins
  // link no GitHub, so they get no card at all.
  function githubSec(d) {
    const g = d.github;
    if (!g) return "";
    const r = g.repo, m = g.metrics, sg = g.signals || {}, ch = g.change || {};
    const href = safeUrl(r ? r.url : g.link);
    const name = r ? r.name : (g.link || "GitHub");
    const status = { pending: "Waiting for the first read of this repo.", missing: "The repo this token links is gone.",
      empty: "The linked account has no public repos.", stale: g.as_of ? `This read is ${ago(g.as_of).replace(" ago", "")} old.` : "" }[g.status];
    const plus = (v) => (v == null ? "" : ` <span class="delta ${v > 0 ? "up" : v < 0 ? "down" : "flat"}">${v > 0 ? "+" : ""}${int(v)}</span>`);
    const gist = m ? `${int(m.stars)} ★${m.commits_7d != null ? ` · ${int(m.commits_7d)} commits/7d` : ""}` : esc(g.status);
    const lvl = { bad: "", warn: "soft", good: "soft", info: "more" };
    const days = (n) => plural(Math.max(1, Math.round(Math.abs(n))), "day");
    const facts = r ? [r.language, r.license && r.license !== "Other" ? r.license : null,
      r.created_at ? `created ${ago(r.created_at)}` : null,
      sg.age_at_launch_days != null ? `${days(sg.age_at_launch_days)} ${sg.age_at_launch_days >= 0 ? "before" : "after"} the token launched` : null]
      .filter(Boolean).map(esc).join(" · ") : "";
    const notOwn = g.own === false ? { account: "The token links an account, not a repo; this is the account's most-starred repo.",
      older: "This repo is older than the token and doesn't carry its contract or ticker: any coin can link one. Shown for reference, not judged." }[g.not_own]
      || "Not shown to be the token's own repo: shown for reference, not judged." : "";
    const perDay = sg.stars_per_day == null ? "" : sg.stars_per_day >= 1 ? `${int(sg.stars_per_day)} a day since created` : "under 1 a day since created";
    return sec("github", "GitHub", gist, g.as_of, `${status ? `<p class="dz-flagline">${esc(status)}</p>` : ""}${notOwn ? `<p class="dz-flagline">${esc(notOwn)}</p>` : ""}
      <p class="dz-say">${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(name)} ↗</a>` : esc(name)}${r?.fork && r.parent
        ? ` <span class="dz-per">a fork of ${esc(r.parent)}${r.parent_stars != null ? ` (${int(r.parent_stars)} ★)` : ""}</span>` : ""}</p>
      ${r?.description ? `<p class="dz-gh-desc">${esc(r.description)}</p>` : ""}
      ${facts ? `<p class="dz-per">${facts}</p>` : ""}
      ${m ? kv([["Stars", `${int(m.stars)}${plus(ch.h24?.stars)}`, ch.d7?.stars != null ? `${ch.d7.stars > 0 ? "+" : ""}${int(ch.d7.stars)} in 7 days` : perDay],
        ["Forks", int(m.forks), ""], ["Commits", `${int(m.commits_7d)} <span class="dz-per">7d</span>`, m.commits_30d != null ? `${int(m.commits_30d)} in 30 days` : ""],
        ["Contributors", int(m.contributors), ""], ["Last push", m.pushed_at ? esc(ago(m.pushed_at)) : "–", sg.days_since_push != null && sg.days_since_push > 30 ? "quiet" : ""],
        m.release ? ["Latest release", esc(m.release.tag), m.release.at ? esc(ago(m.release.at)) : ""] : null]) : ""}
      ${(g.flags || []).length ? `<div class="dz-flags">${g.flags.map((f) => `<span class="dz-flag ${g.own === false ? "more" : lvl[f.level] ?? "soft"}" title="${esc(f.level)}">${esc(f.text)}</span>`).join("")}</div>` : ""}
      <p class="dz-per">Public metadata from GitHub's API; the code itself is never downloaded.</p>`);
  }

  // FOMO's ranked traders on this coin (albatross/deepend/fomo.py, through the unofficial fomoapi.io): who holds it or
  // traded it in the last 7 days, top traders first. Handles and theses are FOMO users' own words: shown escaped,
  // never linked, never sent to a model. No card for a coin no FOMO trader has touched.
  const FOMO_ACT = { buy: "bought", sell: "sold out", trim: "trimmed", holds: "holds" };
  const FOMO_USD = { fill: "trade", position: "position", holding: "holding" };
  const FOMO_SHOWN = 10;
  // FOMO's alert text starts "<handle> posted a thesis on $SYM:"; the card names the author underneath already.
  const thesisText = (x) => String(x.text || "").replace(/^.{1,60}? posted a thesis on [^:]{1,40}:\s*/i, "");
  // A trader's own record, once the grader has some of their buys (deep_grade: FOMO is its own group).
  const record = (r) => (r && r.graded >= 3 ? `<span class="dz-sub">2x on ${int(r.hit_2x)} of ${plural(r.graded, "graded buy")}</span>` : "");
  function fomoSec(d) {
    const f = d.fomo;
    if (!f) return "";
    const shown = (f.traders || []).slice(0, FOMO_SHOWN), more = Math.max(0, (f.n_traders || 0) - shown.length);
    const rows = shown.map((t) => `<tr><td class="left"><b>${esc(t.handle)}</b>${t.rank != null ? ` <span class="dz-htag${t.top ? " t-top" : ""}">#${int(t.rank)} ${esc(t.board || "")}</span>` : ""}${
        t.is_dev ? ` <span class="dz-htag t-dev">dev</span>` : ""}${t.clan ? `<span class="dz-sub">clan ${esc(t.clan)}</span>` : ""}${record(t.record)}</td>
      <td class="left"><span class="dz-act a-${esc(t.action || "none")}">${esc(FOMO_ACT[t.action] || t.action || "–")}</span>${t.at && t.action !== "holds" ? `<span class="dz-sub">${when(t.at)}</span>` : ""}</td>
      <td>${t.usd != null ? usd(t.usd) : "–"}${t.usd != null && FOMO_USD[t.usd_kind] ? `<span class="dz-sub">${FOMO_USD[t.usd_kind]}</span>` : ""}</td></tr>`).join("");
    const theses = (f.theses || []).map((x) => `<li class="dz-thesis"><p>${esc(thesisText(x))}</p><div class="dz-postfoot">${esc(x.handle)}${x.rank != null ? ` · #${int(x.rank)}` : ""} · ${
      x.at ? when(x.at) : "on their position"}<button class="dz-readall" type="button" aria-expanded="false" hidden>Read all</button></div></li>`).join("");
    const clans = (f.clans || []).map((c) => `<span class="dz-htag">${esc(c.name)} · ${plural(c.traders, "trader")}${c.top ? `, ${int(c.top)} top 50` : ""}</span>`).join("");
    const stale = f.holders_stale && f.holders_as_of ? `<p class="dz-flagline">FOMO's holder list is ${esc(ago(f.holders_as_of).replace(" ago", ""))} old.</p>` : "";
    const gist = `${plural(f.n_traders, "trader")}${f.n_top ? ` · ${int(f.n_top)} top 50` : ""}`;
    return sec("fomo", "FOMO traders", gist, f.as_of, `${stale}
      ${kv([["Traders", int(f.n_traders), `held it or traded it in ${int(f.window_h / 24)} days`], ["Top 50", int(f.n_top), "on FOMO's 7-day or 30-day board"],
        ["Buys · sells", `${int(f.buys)} · ${int(f.sells)}`, `${plural(f.buyers, "buyer")}, ${plural(f.sellers, "seller")}`]])}
      ${rows ? `<div class="table-wrap bare"><table class="plain dz-t dz-fomo"><thead><tr><th class="left">Trader</th><th class="left">Last move</th>
        <th>Value</th></tr></thead><tbody>${rows}</tbody></table></div>
        ${more ? `<p class="dz-per">${plural(more, "more trader")} not shown; top traders come first, then the largest holdings.</p>` : ""}` : ""}
      ${clans ? `<h3 class="dz-h3">FOMO clans on it <span class="cat">groups its traders belong to</span></h3><div class="dz-kols">${clans}</div>` : ""}
      ${theses ? `<h3 class="dz-h3">Theses <span class="cat">their own words, unverified</span></h3><ul class="xposts dz-posts">${theses}</ul>` : ""}
      <p class="dz-per">From FOMO's app through fomoapi.io, an unofficial relay. Its feed shows trades and positions of about $3K or more. A rank is the trader's best on FOMO's 7-day or 30-day board.</p>`);
  }

  // Reddit, read through Google search (albatross/deepend/reddit.py): every thread shown is one Google's own link led
  // to, so a link here is always a real reddit.com thread. No card for a coin that was never read.
  function redditSec(d) {
    const r = d.reddit;
    if (!r) return "";
    const note = r.status === "stale" ? `<p class="dz-flagline">This read is ${esc(ago(r.as_of).replace(" ago", ""))} old.</p>` : "";
    if (!r.found) return sec("reddit", "Reddit", "no threads returned", r.as_of, `${note}${empty("No Reddit thread came back.", "Our searches returned no Reddit thread about this coin.")}`);
    const link = (u) => (/^https:\/\/(www\.|old\.)?reddit\.com\//.test(u || "") ? u : null);
    const flags = (r.red_flags || []).map((f) => { const u = link(f.url); return `<li class="${["rug", "scam", "impersonation"].includes(f.kind) ? "" : "soft"}"><b>${esc(f.kind === "other" ? "concern" : f.kind)}</b>${esc(f.detail)}${u ? ` <a href="${esc(u)}" target="_blank" rel="noopener noreferrer">thread ↗</a>` : ""}</li>`; }).join("");
    const threads = (r.threads || []).map((t) => {
      const u = link(t.url), foot = [t.posted ? esc(t.posted) : null, t.comments != null ? plural(t.comments, "comment") : null, t.score != null ? `${int(t.score)} points` : null].filter(Boolean).join(" · ");
      return `<li class="st-${esc(t.stance || "none")}"><div class="xp-a">${esc(t.subreddit || "")}${t.stance ? `<span class="dz-stance s-${esc(t.stance)}">${esc(t.stance)}</span>` : ""}</div>
        <p>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t.title)} ↗</a>` : esc(t.title)}</p>${foot ? `<div class="dz-postfoot">${foot}</div>` : ""}</li>`;
    }).join("");
    const mood = r.mood != null ? `${moodDab(r.mood / 5)} <span class="dz-moodw">${esc(r.mood_label || "")}</span>` : "–";
    const gist = `${r.mood != null ? `${moodDab(r.mood / 5)} ` : ""}${esc(r.mood_label || "")}${r.activity_label ? ` · ${esc(r.activity_label.toLowerCase())}` : ""}`;
    return sec("reddit", "Reddit", gist, r.as_of, `${note}
      ${flags ? `<h3 class="dz-h3">Red flags</h3><ul class="dz-redflags">${flags}</ul>` : ""}
      ${kv([["Mood", mood, "−5 to +5"], ["Activity", esc(r.activity_label || "–"), `${plural((r.threads || []).length, "thread")}${
        r.independent != null && r.independent !== (r.threads || []).length ? `, ${int(r.independent)} outside its own subreddit` : ""}`]])}
      ${r.summary ? `<p class="dz-say">${esc(r.summary)}</p>` : ""}${r.why_it_matters ? `<p class="xs-why">${esc(r.why_it_matters)}</p>` : ""}
      ${threads ? `<h3 class="dz-h3">Threads</h3><ul class="xposts dz-posts">${threads}</ul>` : ""}
      <p class="dz-per">Read through Google search: each thread is one Google's own link led to. Dates are as the search showed them.</p>`);
  }

  // The wallets around the coin (albatross/deepend/wallets.py): role labels, wallets that trade together, and who
  // funded the fresh ones. Insiders here are RugCheck's for this token only; the token's own dev is under Security.
  const WLABEL = { top_trader: ["ranked trader", "ranked traders"], sniper: ["sniper", "snipers"], insider: ["insider", "insiders"],
    bot: ["bot", "bots"], deployer: ["deployer of another coin", "deployers of other coins"], exchange: ["exchange", "exchanges"],
    service: ["service", "services"], launchpad: ["launchpad", "launchpads"], mass_launch: ["mass launcher", "mass launchers"] };
  const wlabel = (k, n = 1) => (WLABEL[k] ? WLABEL[k][n === 1 ? 0 : 1] : k);
  function walletsSec(d) {
    const w = d.wallets;
    if (!w) return "";
    const chain = d.token.chain, e = w.early, f = w.funding;
    const funders = chain === "solana" && (w.top_holders || []).some((h) => h.first_funder);   // else no column of dashes
    const labels = Object.entries(w.labels || {}).map(([k, n]) => `<span class="dz-htag t-${esc(k)}">${int(n)} ${esc(wlabel(k, n))}</span>`).join("");
    const findings = (w.findings || []).map((x) => `<li>${esc(x)}</li>`).join("");
    // One funder's wallets buying together is a bundle (red); wallets that trade together are, on FOMO, mostly
    // copy-trading (neutral).
    const clusters = (w.clusters || []).map((c) => `<li class="${c.kind === "funding" ? "risk" : "plain"}"><b>${c.kind === "cotrade" ? "Trade together" : "One funder"}</b><span>${int(c.here)} of its ${plural(c.size, "wallet")} are on this coin${
      c.within_min != null ? `; ${int(c.bought)} bought within ${esc(c.within_min)} min of each other` : ""}</span>${c.funder ? `<p>Funded by ${wallet(chain, c.funder)}</p>` : ""}</li>`).join("");
    const rows = (w.top_holders || []).slice(0, 10).map((h) => `<tr><td class="left">${wallet(chain, h.owner)}</td><td>${pct(h.pct)}</td>
      <td class="left">${(h.labels || []).map((l) => `<span class="dz-htag t-${esc(l)}">${esc(wlabel(l))}</span>`).join("") || `<span class="cat">–</span>`}</td>
      ${funders ? `<td class="left">${h.first_funder ? wallet(chain, h.first_funder) : `<span class="cat">–</span>`}</td>` : ""}</tr>`).join("");
    const gist = `${plural(w.wallets, "wallet")}${(w.clusters || []).length ? ` · ${plural(w.clusters.length, "group")}` : ""}`;
    return sec("wallets", "Wallets", gist, w.as_of, `
      ${findings ? `<ul class="dz-findings">${findings}</ul>` : ""}
      ${kv([["Wallets seen", int(w.wallets), "trading or holding it"], e && e.buyers >= 5 ? ["Early snipers", `${int(e.snipers)} of ${int(e.buyers)}`, "the first FOMO buyers we saw"] : null,
        f && f.funders_known ? ["Funders known", `${int(f.funders_known)} of ${int(f.holders)}`, "top holders"] : null])}
      ${labels ? `<div class="dz-kols">${labels}</div>` : ""}
      ${clusters ? `<h3 class="dz-h3">Wallet groups on this coin</h3><ul class="dz-clusters dz-wgroups">${clusters}</ul>` : ""}
      ${rows ? `<h3 class="dz-h3">Top holders <span class="cat">${funders ? "labels and who funded them" : "labels"}</span></h3><div class="table-wrap bare"><table class="plain dz-t"><thead><tr><th class="left">Wallet</th><th>Share</th><th class="left">Labels</th>${
        funders ? `<th class="left">Funded by</th>` : ""}</tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <p class="dz-per">Labels from FOMO's boards, Birdeye and RugCheck${chain === "solana" ? "; a funder is the wallet behind a fresh wallet's first SOL" : ""}. Nothing here counts in the score yet.</p>`);
  }

  function tradesSec(d) {
    const tr = d.traders, m = d.market || {}, bot = d.bot || [];
    const flow = (label, x) => {
      if (!x || x.buys == null) return "";
      const n = x.buys + x.sells || 1;
      return `<div class="dz-flow"><span class="k">${label}</span><span class="dz-flowbar" role="img" aria-label="${int(x.buys)} buys, ${int(x.sells)} sells"><i class="b" style="flex-grow:${x.buys / n}"></i><i class="s" style="flex-grow:${x.sells / n}"></i></span>
        <span class="v"><b>${int(x.buys)}</b> buys · <b>${int(x.sells)}</b> sells</span></div>`;
    };
    const flows = m.txns ? `${flow("1 hour", m.txns.h1)}${flow("24 hours", m.txns.h24)}` : "";
    const rows = (tr?.top || []).map((t) => `<tr><td class="left">${wallet(d.token.chain, t.owner)}</td><td><span class="mc-since ${upDown(t.pnl_usd)}">${signedUsd(t.pnl_usd)}</span></td>
      <td>${usd(t.volume_usd)}</td><td class="left">${(t.tags || []).map(tagChip).join(" ") || `<span class="cat">–</span>`}</td></tr>`).join("");
    const real = bot.filter((x) => !x.dry);
    // four columns, so the table fits the narrow column on wide screens and a phone alike
    const botRows = [...bot].reverse().slice(0, 20).map((x) => `<tr><td class="left">${x.entry_at ? when(x.entry_at) : "–"}<span class="dz-sub">${esc(x.strategy || "")}${x.dry ? " (paper)" : ""}</span></td>
      <td>${usd(x.size_usd)}<span class="dz-sub">at ${price(x.entry_price)}</span></td>
      <td class="left">${esc(x.status || "–")}${x.realized_x != null ? `<span class="dz-sub">${x.realized_x.toFixed(2)}× realised</span>` : ""}</td>
      <td>${x.real_pnl_usd == null ? "–" : `<span class="mc-since ${upDown(x.real_pnl_usd)}">${x.real_pnl_usd >= 0 ? "+" : "−"}$${Math.abs(x.real_pnl_usd).toFixed(2)}</span>`}</td></tr>`).join("");
    if (!tr && !flows && !botRows) return sec("trades", "Trades", "", null, notYet(d, "trader breakdown"));
    const gist = m.txns?.h1 ? `${int(m.txns.h1.buys)} buys / ${int(m.txns.h1.sells)} sells 1h${m.net_buyers_h1 != null ? ` · ${int(m.net_buyers_h1)} net buyers` : ""}`
      : real.length ? plural(real.length, "bot position") : "";
    return sec("trades", "Trades", gist, tr?.as_of || m.as_of, `
      ${flows ? `<div class="dz-flows">${flows}</div>` : ""}
      ${m.traders || m.organic_score != null || tr ? kv([m.traders ? ["Traders 1h", int(m.traders.h1), m.traders.h24 != null ? `24h ${int(m.traders.h24)}` : ""] : null,
        m.net_buyers_h1 != null ? ["Net buyers 1h", int(m.net_buyers_h1), "more wallets buying than selling"] : null,
        m.organic_score != null ? ["Organic volume", `${Math.round(m.organic_score)}<small>/100</small>`, esc(m.organic_label || "")] : null,
        tr ? ["Smart money holding", int(tr.smart_holders), "wallets we label profitable"] : null]) : ""}
      ${rows ? `<h3 class="dz-h3">Top traders <span class="cat">by profit on this token</span></h3><div class="table-wrap bare"><table class="plain dz-t"><thead><tr><th class="left">Wallet</th><th>P&amp;L</th><th>Volume</th><th class="left">Tags</th></tr></thead><tbody>${rows}</tbody></table></div>` : tr ? "" : notYet(d, "top-trader list")}
      ${botRows ? `<h3 class="dz-h3">The bot's trades <span class="cat">${plural(bot.length, "position")}${bot.length > 20 ? ", the 20 newest" : ""}</span></h3><div class="table-wrap bare"><table class="plain dz-t"><thead><tr><th class="left">Entry</th><th>Size</th><th class="left">Status</th><th>P&amp;L</th></tr></thead><tbody>${botRows}</tbody></table></div>` : ""}`);
  }

  function securitySec(d) {
    const s = d.security;
    if (!s) return sec("security", "Security", "", null, notYet(d, "security read"));
    const yn = (v, good, bad) => (v == null ? `<span class="dz-yn u"><i>?</i>unknown</span>` : v ? `<span class="dz-yn g"><i>✓</i>${good}</span>` : `<span class="dz-yn b"><i>✕</i>${bad}</span>`);
    const checks = [
      ["Mint authority", yn(s.mint_revoked, "revoked", "still live: more can be minted")],
      ["Freeze authority", yn(s.freeze_revoked, "revoked", "still live: wallets can be frozen")],
      ["LP locked", s.lp_locked_pct == null ? `<span class="cat">unknown</span>` : pct(s.lp_locked_pct, 0)],
      ["Transfer fee", s.transfer_fee == null ? `<span class="cat">unknown</span>` : s.transfer_fee ? `<span class="dz-yn b"><i>✕</i>${typeof s.transfer_fee === "number" ? pct(s.transfer_fee) : "yes"}</span>` : `<span class="dz-yn g"><i>✓</i>none</span>`],
      ["Insiders", s.insiders == null ? `<span class="cat">unknown</span>` : plural(s.insiders, "wallet")],
      ["Top 10 hold", pct(s.top10_pct)],
    ];
    if (s.honeypot) checks.push(["Honeypot", yn(s.honeypot.is_honeypot == null ? null : !s.honeypot.is_honeypot, "sells go through", "sells fail")],
      ["Buy / sell tax", `${pct(s.honeypot.buy_tax)} / ${pct(s.honeypot.sell_tax)}`]);
    const risks = (s.rugcheck?.risks || []).map((r) => `<li class="lv-${esc(String(r.level || "").toLowerCase())}"><b>${esc(r.name)}</b>${r.level ? ` <span class="dz-lv">${esc(r.level)}</span>` : ""}${r.description ? `<p>${esc(r.description)}</p>` : ""}</li>`).join("");
    const dv = s.dev;
    const gist = [s.mint_revoked != null ? `mint ${s.mint_revoked ? "revoked" : "live"}` : "", s.freeze_revoked != null ? `freeze ${s.freeze_revoked ? "revoked" : "live"}` : "",
      s.lp_locked_pct != null ? `LP ${pct(s.lp_locked_pct, 0)} locked` : "", dv?.launches != null ? `dev: ${plural(dv.launches, "launch", "launches")}` : ""].filter(Boolean).join(" · ");
    return sec("security", "Security", esc(gist), s.as_of || d.freshness?.security, `
      <dl class="dz-checks">${checks.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${dv ? `<h3 class="dz-h3">The deployer</h3>${kv([["Wallet", dv.address ? wallet(d.token.chain, dv.address) : "–", ""], ["Launches", int(dv.launches), dv.migrated != null ? `${int(dv.migrated)} migrated${dv.launches ? ` (${pct((dv.migrated / dv.launches) * 100, 1)})` : ""}` : ""],
        ["Still holds", pct(dv.balance_pct), "of the supply"]])}` : ""}
      <h3 class="dz-h3">RugCheck ${s.rugcheck?.score != null ? `<span class="cat">score ${esc(s.rugcheck.score)}</span>` : ""}</h3>
      ${s.rugcheck ? (risks ? `<ul class="dz-risks">${risks}</ul>` : `<p class="dz-okline">No risks flagged.</p>`) : `<p class="cat">No RugCheck report.</p>`}`);
  }

  function newsSec(d) {
    const n = d.news;
    if (n == null) return sec("news", "News", "", null, notYet(d, "news"));
    if (!n.length) return sec("news", "News", "none", D.at, empty(`No headlines mention ${esc(d.token.symbol || "this token")}.`, "Most micro-caps never make the news."));
    // How a headline names the token matters: a ticker alone can be another coin's.
    const MATCH = { address: "names its address", name: "names it", ticker: "ticker only: may be another coin" };
    const votes = (v) => (v && typeof v === "object" ? [v.positive ? `${int(v.positive)} bullish` : "", v.negative ? `${int(v.negative)} bearish` : "",
      v.important ? `${int(v.important)} important` : ""].filter(Boolean).join(", ") : "");
    const items = n.map((x) => {
      const u = safeUrl(x.url), vt = votes(x.votes);
      return `<li>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(x.title)} ↗</a>` : `<span>${esc(x.title)}</span>`}
        <span class="cat">${[esc(x.source || ""), x.published_at ? when(x.published_at) : "", vt ? `votes: ${vt}` : "",
          x.match ? `<span class="dz-match m-${esc(x.match)}">${esc(MATCH[x.match] || x.match)}</span>` : ""].filter(Boolean).join(" · ")}</span></li>`;
    }).join("");
    return sec("news", "News", plural(n.length, "headline"), D.at, `<ul class="dz-news">${items}</ul>`);
  }

  // ─── events on the chart (2026-10-06) ─────────────────────────────────────
  // Every X post, X warning, group call, FOMO trader, red flag raised or cleared and bot buy, as a bubble over its bar.
  // A hover, a tap or focus shows its card and a click pins it. Bubbles that would overlap become one with a count, and
  // its card lists them all. They follow the chart through scrolls, zooms, resizes and new candles.
  // kind: [the card's word, the toggle it belongs to, its rank when one bubble stands for several, its lane]: the
  // Bot buys and sales sit below the candles, apart from everything else above them, and a sale next to a
  // buy takes the row under it (a buy and a sale are never one bubble)
  const MK = {
    flag: ["Red flag raised", "flag", 0], xflag: ["X warning", "x", 1], call: ["Telegram call", "call", 2], x: ["X post", "x", 3],
    fomo: ["FOMO trader", "fomo", 4], bot: ["Bot buy", "bot", 0, "buy"], bot_sale: ["Bot sale", "bot", 0, "sell"],
    flag_clear: ["Red flag cleared", "flag", 6] };
  const MK_GROUPS = [["x", "X posts"], ["call", "Calls"], ["fomo", "FOMO traders"], ["flag", "Red flags"], ["bot", "Bot trades"]];
  const MK_SIZE = 26;
  const MKS = { clusters: [], keys: new Map(), open: null, pinned: false, hideT: 0, raf: 0 };
  const mkOff = (() => { try { return new Set(JSON.parse(localStorage.getItem("albatross.marksOff") || "[]")); } catch { return new Set(); } })();
  const TYPE_WORD = { shill: "shill", bot: "bot", project: "the project's account" };
  const FACET_WORD = { contract: "Shared the contract address", talk: "Mentioned the ticker", account: "Posted by the project's account" };
  const compact = (n) => (n == null ? "" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
  const statusId = (u) => (String(u || "").match(/\/status\/(\d+)/) || [])[1] || null;

  function chartEvents() {
    const out = [];
    for (const m of D.candles?.markers || []) if (MK[m.kind]) out.push({ ...m, key: `${m.kind}:${m.id || `${m.t}:${m.label}`}` });
    for (const f of D.data?.fomo?.traders || []) {
      const t = ms(f.at);
      if (t) out.push({ t, kind: "fomo", label: f.display_name || f.handle || `#${f.rank}`, price: null, key: `fomo:${f.handle || f.display_name}:${t}`, detail: f });
    }
    return out;
  }
  const mkClass = (e) => `k-${e.kind}${e.kind === "x" ? ` s-${e.detail?.stance || "neutral"}${["shill", "bot"].includes(e.detail?.type) ? " t-junk" : ""}`
    : e.kind === "bot_sale" && e.detail?.pnl_usd != null ? (e.detail.pnl_usd < 0 ? " loss" : " gain") : e.detail?.earlier ? " earlier" : ""}`;
  function mkGlyph(e) {
    if (e.kind === "x") return ((e.detail?.author || e.label || "?").replace(/^@/, "")[0] || "?").toUpperCase();
    if (e.kind === "fomo") return e.detail?.rank != null && e.detail.rank < 100 ? String(e.detail.rank) : ((e.label || "F")[0] || "F").toUpperCase();
    return { xflag: "!", call: "✈", flag: "!", flag_clear: "✓", bot: "▲", bot_sale: "▼" }[e.kind] || "•";
  }
  const relayMarks = () => { if (!MKS.raf) MKS.raf = requestAnimationFrame(() => { MKS.raf = 0; layoutMarks(); }); };

  function renderKinds() {
    const host = $("#dz-mk-kinds");
    if (!host) return;
    const n = {};
    if (D.candles?.bars.length > 1 && D.chart && !$("#dz-chart").hidden) for (const e of chartEvents()) n[MK[e.kind][1]] = (n[MK[e.kind][1]] || 0) + 1;
    host.innerHTML = MK_GROUPS.filter(([g]) => n[g]).map(([g, w]) => `<button type="button" class="dz-mk-kind k-${g}${mkOff.has(g) ? "" : " on"}" data-mk-kind="${g}"
      aria-pressed="${!mkOff.has(g)}" title="${mkOff.has(g) ? "Show" : "Hide"} these on the chart">${esc(w)} <b>${int(n[g])}</b></button>`).join("");
    host.hidden = !host.innerHTML;
  }

  function layoutMarks() {
    const host = $("#dz-marks"), c = D.candles, ch = D.chart;
    if (!host) return;
    if (!c || !ch || c.bars.length < 2 || $("#dz-chart").hidden) { host.innerHTML = ""; MKS.clusters = []; MKS.keys = new Map(); hideCard(true); return; }
    const el = $("#dz-chart"), ts = ch.chart.timeScale(), w = el.clientWidth - ch.chart.priceScale("right").width(), h = el.clientHeight;
    const times = c.bars.map((b) => b.time), step = TF_MIN[D.tf] * 60;
    const barOf = (t) => {              // the bar it happened in, or the last one before it
      const s = Math.floor(t / 1000 / step) * step;
      if (s < times[0]) return -1;
      let lo = 0, hi = times.length - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (times[mid] <= s) lo = mid; else hi = mid - 1; }
      return lo;
    };
    const lanes = { above: new Map(), buy: new Map(), sell: new Map() };
    for (const e of chartEvents()) {
      if (mkOff.has(MK[e.kind][1])) continue;
      const i = e.detail?.earlier ? 0 : barOf(e.t), lane = lanes[MK[e.kind][3] || "above"];   // bought before the chart: its edge
      if (i < 0) continue;
      if (!lane.has(i)) lane.set(i, []);
      lane.get(i).push(e);
    }
    const clusters = [];
    for (const [name, byBar] of Object.entries(lanes)) {
      const pts = [];
      for (const [i, evs] of byBar) {
        const x = ts.timeToCoordinate(times[i]);
        if (x != null && x >= -2 && x <= w + 2) pts.push({ i, x, evs });
      }
      pts.sort((a, b) => a.x - b.x);
      let last = null;
      for (const p of pts) {             // bars closer than a bubble share one (with room for a count badge)
        if (last && p.x - last.xs[0] < MK_SIZE + 12) { last.xs.push(p.x); last.bars.push(p.i); last.evs.push(...p.evs); }
        else clusters.push(last = { lane: name, xs: [p.x], bars: [p.i], evs: [...p.evs] });
      }
    }
    const keys = new Map();
    host.innerHTML = clusters.map((cl, n) => {
      cl.evs.sort((a, b) => MK[a.kind][2] - MK[b.kind][2] || b.t - a.t);
      const x = cl.xs.reduce((a, v) => a + v, 0) / cl.xs.length;
      const top = Math.min(...cl.bars.map((i) => ch.candles.priceToCoordinate(c.bars[i].high) ?? h));
      const bottom = Math.max(...cl.bars.map((i) => ch.candles.priceToCoordinate(c.bars[i].low) ?? 0));
      const row = cl.lane === "sell" && clusters.some((o) => o.lane === "buy" && Math.abs(o.xs[0] - cl.xs[0]) < MK_SIZE + 12) ? MK_SIZE + 6 : 0;
      const up = top - 8 - MK_SIZE / 2 - row, down = bottom + 8 + MK_SIZE / 2 + row;
      let y = cl.lane === "above" ? (up < MK_SIZE / 2 + 4 ? down : up) : (down > h - MK_SIZE / 2 - 4 ? up : down);   // no room: the other side
      y = Math.min(Math.max(y, MK_SIZE / 2 + 2), h - MK_SIZE / 2 - 2);
      Object.assign(cl, { x, y, n, key: cl.evs[0].key });
      cl.evs.forEach((e) => keys.set(e.key, n));
      const lead = cl.evs[0], more = cl.evs.length > 1 ? `, and ${cl.evs.length - 1} more` : "";
      return `<button type="button" class="dz-mk ${mkClass(lead)}${MKS.open === cl.key ? " on" : ""}" style="left:${x.toFixed(1)}px;top:${y.toFixed(1)}px" data-mk="${n}"
        aria-label="${esc(`${MK[lead.kind][0]}: ${lead.label || ""}${more}`)}">${esc(mkGlyph(lead))}${cl.evs.length > 1 ? `<i>${cl.evs.length > 99 ? "99+" : cl.evs.length}</i>` : ""}</button>`;
    }).join("");
    MKS.clusters = clusters;
    MKS.keys = keys;
    const open = MKS.open && clusters.find((cl) => cl.key === MKS.open);
    if (open) placeCard(open);
    else if (MKS.open) hideCard(true);       // scrolled off, or turned off
  }

  function cardRow(e) {
    const d = e.detail || {}, iso = new Date(e.t).toISOString();
    const time = `<span class="mk-t" title="${esc(utc(iso))}">${esc(ago(iso))}</span>`;
    const av = `<span class="dz-mk mk-av ${mkClass(e)}" aria-hidden="true">${esc(mkGlyph(e))}</span>`;
    const link = (u, w) => (/^https:\/\/(x|twitter)\.com\//.test(u || "") ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${w} ↗</a>` : "");
    if (e.kind === "x") {
      const big = d.well_known || (d.likes ?? 0) >= 100;
      const tags = `${d.stance ? `<span class="dz-stance s-${esc(d.stance)}">${esc(d.stance)}</span>` : ""}${
        d.type && d.type !== "organic" ? `<span class="mk-tag t-${esc(d.type)}">${esc(TYPE_WORD[d.type] || d.type)}</span>` : ""}${big ? `<span class="mk-tag big">large account</span>` : ""}`;
      return `<div class="mk-row"><div class="mk-who">${av}<b>${esc(d.author || e.label)}</b>${d.followers != null ? `<span class="cat">${esc(compact(d.followers))} followers</span>` : ""}${time}</div>
        ${d.text ? `<p>${esc(d.text)}</p>` : `<p class="cat">${esc(FACET_WORD[d.facet] || "Mentioned it")}. We didn't save this post's text; open it on X to read it.</p>`}
        <div class="mk-foot">${tags}${d.likes != null ? `<span class="cat">${plural(d.likes, "like")}</span>` : ""}${link(d.url, "Open on X")}</div></div>`;
    }
    if (e.kind === "xflag") return `<div class="mk-row"><div class="mk-who">${av}<b>${esc(e.label)}</b>${time}</div><p>${esc(d.detail || "")}</p>
      <div class="mk-foot">${d.author ? `<span class="cat">in a post by ${esc(d.author)}</span>` : ""}${link(d.url, "The post")}</div></div>`;
    if (e.kind === "call") {
      const last = D.candles?.bars.at(-1)?.close, since = e.price && last ? (last / e.price - 1) * 100 : null, who = d.caller || e.who;
      return `<div class="mk-row"><div class="mk-who">${av}<b>${esc(e.label || "a group")}</b><span class="cat">Telegram call</span>${time}</div>
        <p>${who ? `${esc(who)} · ` : ""}${d.intent && d.intent !== "mention" ? `${esc(d.intent)} call` : "called it"}${e.price ? ` at ${esc(price(e.price))}` : ""}${
          since != null ? `; now <span class="mc-since ${upDown(since)}">${sinceFmt(since)}</span>` : ""}</p></div>`;
    }
    if (e.kind === "fomo") {
      const act = { buy: "bought", sell: "sold", add: "added", trim: "trimmed" }[d.action] || d.action || "traded";
      const rank = d.rank != null ? `rank #${int(d.rank)}${d.board ? ` on the ${esc(d.board)} board` : ""}` : "";
      return `<div class="mk-row"><div class="mk-who">${av}<b>${esc(e.label)}</b><span class="cat">${["FOMO", rank, d.handle && d.handle !== e.label ? `@${esc(d.handle)}` : "",
        d.clan ? esc(d.clan) : ""].filter(Boolean).join(" · ")}</span>${time}</div>
        <p>${esc(act)}${d.holding_usd != null ? ` · holds ${esc(usd(d.holding_usd))}` : ""}${d.position_pnl_usd != null ? ` · position ${esc(signedUsd(d.position_pnl_usd))}` : ""}${
          d.pnl_30d != null ? ` · 30-day P&amp;L ${esc(signedUsd(d.pnl_30d))}` : ""}${d.is_dev ? " · the token's developer" : ""}</p></div>`;
    }
    if (e.kind === "flag" || e.kind === "flag_clear") return `<div class="mk-row"><div class="mk-who">${av}<b>${e.kind === "flag" ? "Red flag" : "Cleared"}: ${esc(e.label)}</b>${time}</div>
      ${d.value || d.first ? `<p>${esc(d.value || "")}${d.first ? `${d.value ? " · " : ""}on its first check` : ""}</p>` : ""}</div>`;
    const acct = d.account ? `<span class="cat">account ${esc(d.account)}</span>` : "";
    const sol = (v) => (v == null ? "" : `${Math.abs(v) < 1 ? Math.abs(v).toFixed(4) : Math.abs(v).toFixed(2)} SOL`);
    const tx = /^[1-9A-HJ-NP-Za-km-z]{60,100}$/.test(d.sig || "") ? `<a href="https://solscan.io/tx/${esc(d.sig)}" target="_blank" rel="noopener noreferrer">The transaction ↗</a>` : "";
    if (e.kind === "bot_sale") {
      const pnl = d.pnl_usd == null ? "" : Math.abs(d.pnl_usd) < 100 ? `${d.pnl_usd < 0 ? "−" : "+"}$${Math.abs(d.pnl_usd).toFixed(2)}` : signedUsd(d.pnl_usd);
      const res = [d.share != null && !d.reason ? `${Math.round(d.share * 100)}% of the position` : "", d.sol != null ? `for ${sol(d.sol)}` : "",
        d.realized_x != null ? `${xs(d.realized_x)} the buy overall` : "", pnl ? `${pnl}${d.size_usd ? ` on ${usd(d.size_usd)}` : ""}` : "",
        d.held_s != null ? `held ${dur(d.held_s)}` : ""].filter(Boolean).join(" · ");
      return `<div class="mk-row"><div class="mk-who">${av}<b>${esc(d.title || "Our bot sold")}</b><span class="cat">${esc(e.label || "")}</span>${time}</div>
        ${res ? `<p>${res}</p>` : ""}${d.bought_at ? `<p class="cat">Bought ${esc(utc(d.bought_at))}${d.buy_price ? ` at ${esc(price(d.buy_price))}` : ""}${d.caller ? `, on ${esc(d.caller)}'s call` : ""}.</p>` : ""}
        <div class="mk-foot">${d.reason ? `<span class="cat">${esc(d.reason)}</span>` : ""}${acct}${tx}</div></div>`;
    }
    if (d.earlier) return `<div class="mk-row"><div class="mk-who">${av}<b>Bought before this chart begins</b><span class="cat">${esc(e.label || "")}</span></div>
      <p>${esc(utc(d.bought_at))} (${esc(ago(d.bought_at))})${e.price ? ` at ${esc(price(e.price))}` : ""}${d.size_usd ? ` · ${esc(usd(d.size_usd))}` : ""}${d.caller ? ` · on ${esc(d.caller)}'s call` : ""}.
        A longer timeframe shows it in place.</p>${acct || tx ? `<div class="mk-foot">${acct}${tx}</div>` : ""}</div>`;
    return `<div class="mk-row"><div class="mk-who">${av}<b>Our bot bought</b><span class="cat">${esc(e.label || "")}</span>${time}</div>
      <p>${e.price ? `at ${esc(price(e.price))}` : ""}${d.size_usd ? ` · ${esc(usd(d.size_usd))}` : ""}${d.sol != null ? ` (${esc(sol(d.sol))})` : ""}${d.caller ? ` · on ${esc(d.caller)}'s call` : ""}</p>
      ${acct || tx ? `<div class="mk-foot">${acct}${tx}</div>` : ""}</div>`;
  }
  function cardHtml(cl) {
    const n = cl.evs.length, shown = cl.evs.slice(0, 40);
    return `<div class="mk-card-h"><span>${n === 1 ? esc(MK[cl.evs[0].kind][0]) : `${int(n)} events here`}</span><button type="button" class="mk-x" data-mk-close aria-label="Close">×</button></div>
      <div class="mk-rows">${shown.map(cardRow).join("")}${n > shown.length ? `<p class="cat">and ${int(n - shown.length)} more: zoom in to see them apart.</p>` : ""}</div>`;
  }
  function placeCard(cl) {
    const card = $("#dz-mk-card"), wrap = $("#dz-chart-wrap"), W = wrap.clientWidth, H = wrap.clientHeight, cw = Math.min(340, W - 16);
    card.style.width = `${cw}px`;
    card.style.left = `${Math.min(Math.max(cl.x - cw / 2, 8), W - cw - 8)}px`;
    if (cl.y < H / 2) Object.assign(card.style, { top: `${cl.y + MK_SIZE / 2 + 8}px`, bottom: "auto", maxHeight: `${Math.max(120, H - cl.y - MK_SIZE / 2 - 16)}px` });
    else Object.assign(card.style, { top: "auto", bottom: `${H - cl.y + MK_SIZE / 2 + 8}px`, maxHeight: `${Math.max(120, cl.y - MK_SIZE / 2 - 16)}px` });
  }
  function showCard(n, pin = false) {
    const cl = MKS.clusters[n], card = $("#dz-mk-card");
    if (!cl) return;
    clearTimeout(MKS.hideT);
    if (MKS.open !== cl.key || card.hidden) card.innerHTML = cardHtml(cl);
    MKS.pinned = pin || (MKS.pinned && MKS.open === cl.key);
    MKS.open = cl.key;
    placeCard(cl);
    card.hidden = false;
    card.classList.toggle("pinned", MKS.pinned);
    $("#dz-marks").querySelectorAll(".dz-mk").forEach((b) => b.classList.toggle("on", +b.dataset.mk === n));
  }
  function hideCard(now = false) {
    clearTimeout(MKS.hideT);
    const go = () => {
      const card = $("#dz-mk-card");
      if (card) { card.hidden = true; card.innerHTML = ""; }
      MKS.open = null; MKS.pinned = false;
      $("#dz-marks")?.querySelectorAll(".dz-mk.on").forEach((b) => b.classList.remove("on"));
    };
    if (now) go();
    else MKS.hideT = setTimeout(() => { if (!MKS.pinned) go(); }, 180);
  }
  {
    const marks = $("#dz-marks"), card = $("#dz-mk-card");
    marks.addEventListener("pointerover", (e) => { const b = e.target.closest(".dz-mk"); if (b && !MKS.pinned) showCard(+b.dataset.mk); });
    marks.addEventListener("pointerout", (e) => {
      const b = e.target.closest(".dz-mk");
      if (b && !b.contains(e.relatedTarget) && !card.contains(e.relatedTarget)) hideCard();
    });
    marks.addEventListener("click", (e) => {
      const b = e.target.closest(".dz-mk");
      if (!b) return;
      const cl = MKS.clusters[+b.dataset.mk];
      if (MKS.pinned && cl && cl.key === MKS.open) hideCard(true);
      else showCard(+b.dataset.mk, true);
    });
    marks.addEventListener("focusin", (e) => { const b = e.target.closest(".dz-mk"); if (b && !MKS.pinned) showCard(+b.dataset.mk); });
    marks.addEventListener("focusout", (e) => { if (!card.contains(e.relatedTarget) && !marks.contains(e.relatedTarget) && !MKS.pinned) hideCard(); });
    card.addEventListener("pointerenter", () => clearTimeout(MKS.hideT));
    card.addEventListener("pointerleave", () => { if (!MKS.pinned) hideCard(); });
    card.addEventListener("click", (e) => { if (e.target.closest("[data-mk-close]")) hideCard(true); });
    document.addEventListener("pointerdown", (e) => { if (MKS.open && !card.contains(e.target) && !marks.contains(e.target)) hideCard(true); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && MKS.open) hideCard(true); });
    // a post in the X section lights its bubble on the chart
    dossTab.addEventListener("pointerover", (e) => {
      const li = e.target.closest("[data-mkey]");
      if (li) $(`#dz-marks .dz-mk[data-mk="${MKS.keys.get(li.dataset.mkey)}"]`)?.classList.add("hl");
    });
    dossTab.addEventListener("pointerout", (e) => {
      const li = e.target.closest("[data-mkey]");
      if (li && !li.contains(e.relatedTarget)) $("#dz-marks")?.querySelectorAll(".dz-mk.hl").forEach((b) => b.classList.remove("hl"));
    });
  }

  // ─── "Search X now" (2026-10-06): an X read of this token, past the X Scout's queue ─────
  const ASK_WORD = { read: "X read just now: it's below.", quiet: "Searched X: nothing about it in the window.",
    ungrounded: "Searched X: nothing we could verify.", failed: "The X search failed. Try again in a minute.",
    expired: "The search wasn't picked up within 30 minutes." };
  function xAskHtml(x) {
    const a = x?.ask, mine = D.xask?.key === D.key ? D.xask : null, waiting = (!!a && !a.done_at) || !!mine?.busy;
    const recent = a?.done_at && Date.now() - ms(a.done_at) < 15 * 60e3;
    const said = waiting ? (a && !a.done_at ? `Asked ${ago(a.asked_at)}. A read takes a minute or two, and this section updates by itself.` : "Asking…")
      : recent ? (a.status === "refused" ? `Not searched: ${a.note || "no budget left this week"}.` : ASK_WORD[a.status] || "")
      : "Reads X for this token now, ahead of the X Scout's own queue. About $0.08 of the week's Grok budget.";
    return `<div class="dz-xask"><button type="button" class="dz-run" data-xask${waiting ? " disabled" : ""}>${waiting ? "Searching X…" : "Search X now"}</button>
      <span class="dz-xask-t">${esc(said)}</span>${mine?.err ? `<span class="dz-sr-err">${esc(mine.err)}</span>` : ""}</div>`;
  }
  // While a search waits, its state is asked every 4 seconds (a cheap call), even with the tab in the background, and
  // the dossier reloads the moment the read is in (the server drops its cached dossier when it answers "done").
  let askPoll = 0;
  function watchAsk() {
    clearTimeout(askPoll);
    const a = D.data?.x?.ask, key = D.key;
    if (state.tab !== "dossier" || !a || a.done_at) return;
    askPoll = setTimeout(async () => {
      if (D.key !== key || state.tab !== "dossier") return;
      try {
        const [c, ad] = splitKey(key);
        if ((await get(`/api/deep/x-read/${enc(c)}/${enc(ad)}`)).ask?.done_at) { await loadDossier(); return; }
      } catch { /* the 20-second refresh carries on */ }
      watchAsk();
    }, 4000);
  }

  async function askX() {
    const key = D.key, [chain, address] = splitKey(key);
    D.xask = { key, busy: true, err: null };
    renderDossier();
    try {
      const r = await fetch("/api/deep/x-read", { method: "POST", headers: { "Content-Type": "application/json", "X-Albatross-Action": "1" },
        body: JSON.stringify({ chain, address }) });
      if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); return; }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `the server answered ${r.status}`);
      D.xask = { key, busy: false, err: null };
      if (D.key === key) await loadDossier();
    } catch (err) {
      D.xask = { key, busy: false, err: `Couldn't ask: ${err.message}.` };
      if (D.key === key) renderDossier();
    }
  }

  function renderDossier() {
    const d = D.data;
    if (!d) return;
    banner();
    $("#dz-page").hidden = false;
    $("#dz-head").innerHTML = headHtml(d);
    $("#dz-ev").innerHTML = evidenceHtml(d);
    $("#dz-stats").innerHTML = statsHtml(d);
    $("#dz-secs-l").innerHTML = [securitySec(d), fitSec(d), githubSec(d), tradesSec(d), fomoSec(d)].join("");
    $("#dz-secs-r").innerHTML = [xSec(d), holdersSec(d), walletsSec(d), redditSec(d), groupsSec(d), newsSec(d)].join("");
    fitTheses();
    const t = d.token, m = d.market || {}, sym = t.symbol || short(t.address);
    const nm = t.name && t.name.trim().toLowerCase() !== (t.symbol || "").trim().toLowerCase() ? t.name : "";
    if (state.deep.label !== sym) { state.deep.label = sym; renderSubnav(); }
    document.dispatchEvent(new CustomEvent("dossierloaded", { detail: { key: D.key, symbol: sym.length > 18 ? `${sym.slice(0, 17)}…` : sym,
      price: m.price != null ? price(m.price) : null, chg: m.change?.h1 ?? null,
      facts: [nm, CHAIN_NAME[t.chain] || t.chain, t.launchpad, t.age_s != null ? `${dur(t.age_s)} old` : ""] } }));
    if (D.candles) { note(); renderKinds(); relayMarks(); }     // the FOMO traders come with the dossier
    watchAsk();
  }

  // ─── the chart (TradingView Lightweight Charts, loaded on first use) ─────
  let lwLoading = null;
  function lwLib() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    lwLoading ??= new Promise((resolve, reject) => {
      const s = Object.assign(document.createElement("script"), { src: LWC_SRC, async: true, crossOrigin: "anonymous", integrity: LWC_SRI });
      s.onload = () => (window.LightweightCharts ? resolve(window.LightweightCharts) : reject(new Error("the chart library is missing")));
      s.onerror = () => { lwLoading = null; s.remove(); reject(new Error("the chart library couldn't be loaded")); };
      document.head.append(s);
    });
    return lwLoading;
  }

  async function ensureChart() {
    if (D.chart) return D.chart;
    const L = await lwLib();
    if (D.chart) return D.chart;
    const ink = "#bdb4a5", hair = "rgba(239, 232, 220, 0.16)";
    const chart = L.createChart($("#dz-chart"), {
      autoSize: true,
      layout: { background: { type: "solid", color: "rgba(0, 0, 0, 0)" }, textColor: ink, fontSize: 12,
        fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif", attributionLogo: false },
      grid: { vertLines: { color: "rgba(239, 232, 220, 0.045)" }, horzLines: { color: "rgba(239, 232, 220, 0.06)" } },
      rightPriceScale: { borderColor: hair, scaleMargins: { top: 0.1, bottom: 0.22 } },
      timeScale: { borderColor: hair, timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: L.CrosshairMode.Normal,
        vertLine: { color: "rgba(231, 217, 191, 0.4)", labelBackgroundColor: "#2b2e2f" },
        horzLine: { color: "rgba(231, 217, 191, 0.4)", labelBackgroundColor: "#2b2e2f" } },
      localization: { priceFormatter: price },
      // a vertical swipe on a phone scrolls the page, not the chart
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    });
    // hollow = closed up, filled = closed down, so direction never rests on colour alone
    const candles = chart.addCandlestickSeries({ upColor: "rgba(0, 0, 0, 0)", downColor: "#c0453b", borderUpColor: "#6fa3d8", borderDownColor: "#c0453b",
      wickUpColor: "#6fa3d8", wickDownColor: "#c0453b", priceLineColor: "rgba(231, 217, 191, 0.55)", priceFormat: { type: "custom", formatter: price, minMove: 1e-12 } });
    const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.subscribeCrosshairMove((p) => legend(p?.time ?? null));
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {          // the bubbles follow; the edge pages back
      relayMarks();
      if (r && r.from < 20) loadOlder();
    });
    if (window.ResizeObserver) new ResizeObserver(relayMarks).observe($("#dz-chart"));
    D.chart = { L, chart, candles, vol, lines: [] };
    return D.chart;
  }

  function clearChart() {
    const ch = D.chart;
    if (ch) { ch.candles.setData([]); ch.vol.setData([]); ch.candles.setMarkers([]); ch.lines.forEach((l) => ch.candles.removePriceLine(l)); ch.lines = []; }
    $("#dz-legend").innerHTML = ""; $("#dz-chart-note").innerHTML = "";
    $("#dz-marks").innerHTML = ""; $("#dz-mk-kinds").innerHTML = ""; hideCard(true);
    chartMsg("Loading the candles…");
  }
  function chartMsg(text) {
    const el = $("#dz-chart-msg");
    el.hidden = !text;
    el.textContent = text || "";
  }
  function syncChartControls() {
    dossTab.querySelectorAll("#dz-tf button").forEach((b) => { b.classList.toggle("on", b.dataset.tf === D.tf); b.setAttribute("aria-pressed", b.dataset.tf === D.tf); });
    const lg = $("#dz-log");
    lg.classList.toggle("on", !!D.log);
    lg.setAttribute("aria-pressed", !!D.log);
  }

  // Candles from the evidence engine (§4), else the price pipeline's 1-minute candles and live quotes; either way bucketed
  // here to the timeframe (a no-op when they already are). Bars from where the pool's own candles end (`until`; null = no
  // candles at all) are built from live quotes: they are drawn grey and carry no volume.
  function bucket(rows, tf, until) {
    const step = TF_MIN[tf] * 60, cut = until === null ? -Infinity : ms(until), by = new Map();
    for (const r of rows) {
      if (!r.time || ![r.open, r.high, r.low, r.close].every((v) => v > 0)) continue;
      const time = Math.floor(r.time / step) * step, b = by.get(time), quote = r.quote ?? (cut != null && r.time * 1000 >= cut);
      if (!b) by.set(time, { time, open: r.open, high: r.high, low: r.low, close: r.close, volume: r.volume || 0, quote });
      else Object.assign(b, { high: Math.max(b.high, r.high), low: Math.min(b.low, r.low), close: r.close, volume: b.volume + (r.volume || 0), quote: b.quote && quote });
    }
    return [...by.values()].sort((a, b) => a.time - b.time);
  }
  function normDeep(c, tf) {
    const rows = (c.candles || []).map(([t, o, h, l, cl, v]) => ({ time: Math.floor(ms(t) / 1000), open: o, high: h, low: l, close: cl, volume: v || 0 }))
      .sort((a, b) => a.time - b.time);
    return { bars: bucket(rows, tf, c.until), markers: (c.markers || []).map((m) => ({ ...m, t: ms(m.t) })).filter((m) => m.t), source: c.source, until: c.until };
  }
  function normMicro(m, tf) {
    const rows = (m.candles || []).map((c) => ({ time: Math.floor(ms(c.ts) / 1000), open: c.open, high: c.high, low: c.low, close: c.close,
      volume: c.volume_usd || 0, quote: !!c.quote }));
    const markers = [
      ...(m.calls || []).map((c) => ({ t: ms(c.called_at), kind: "call", label: c.group_name || "a group", price: c.price, who: c.caller })),
      ...(m.trades || []).filter((x) => !x.dry && x.entry_at).map((x) => ({ t: ms(x.entry_at), kind: "bot", label: x.strategy || "bot", price: x.entry_price,
        detail: { size_usd: x.size_usd, caller: x.caller, account: x.trader } })),
      // a close the buy landed for (api/deep.py bot_sale, the same rule)
      ...(m.trades || []).filter((x) => !x.dry && x.status === "closed" && x.closed_at && !/never|no position/.test(x.close_reason || "")).map((x) => ({
        t: ms(x.closed_at), kind: "bot_sale", label: x.strategy || "bot", price: null, id: `${x.trader}:${x.src_id}`,
        detail: { title: saleTitle(x.close_reason || ""), reason: x.close_reason, realized_x: x.realized_x, pnl_usd: x.real_pnl_usd, size_usd: x.size_usd,
          held_s: x.entry_at ? (ms(x.closed_at) - ms(x.entry_at)) / 1000 : null, account: x.trader } })),
    ].filter((x) => x.t);
    return { bars: bucket(rows, tf), markers, source: "pipeline", until: m.candles_until };
  }

  const saleTitle = (why) => (/^(manual|reconciled — manual)/.test(why) ? "Sold by hand" : why.includes("outside the bot") ? "Sold outside the bot"
    : why === "wallet reset" ? "Closed: wallet reset" : "Our bot sold");

  async function loadCandles() {
    if (!state.deep) return;
    if (!D.tf) { D.tf = tfFor(D.data?.token?.age_s); syncChartControls(); }
    const k = D.key, tf = D.tf, seq = ++D.cseq, [chain, address] = splitKey(k);
    let c = null;
    if (D.mode === "deep") {
      try { c = normDeep(await get(`/api/deep/candles/${enc(chain)}/${enc(address)}?tf=${tf}&minutes=${ASK[tf]}`), tf); }
      catch { c = null; }                  // the price pipeline's candles stand in
    }
    if (!c) {
      try {
        const reuse = tf === "1m" && D.micro && Date.now() - D.micro.at < 15_000;
        c = normMicro(reuse ? D.micro.raw : await get(`/api/micro/token/${enc(chain)}/${enc(address)}?minutes=${MICRO_ASK[tf]}`), tf);
      } catch (e) {
        if (seq === D.cseq && k === D.key && !D.candles) chartMsg(`Couldn't load the candles: ${whyFailed(e)}.`);
        return;
      }
    }
    if (seq !== D.cseq || k !== D.key || tf !== D.tf) return;
    c.key = `${k}|${tf}`;
    if (D.hist?.key !== c.key) D.hist = { key: c.key, bars: [], markers: [], done: D.mode !== "deep", busy: false };
    withHistory(c);
    Object.assign(D, { candles: c, candlesAt: Date.now() });
    c.byTime = new Map(c.bars.map((b) => [b.time, b]));
    await drawCandles();
  }

  // ─── older candles as the reader scrolls left (2026-10-06) ───────────────
  // The latest window comes with every refresh; the windows before it are paged in when the view nears the first bar
  // (the candles endpoint's `before`), kept in D.hist and put back in front of each refresh, until one comes back
  // empty. A page's "bought before this chart begins" markers stand for the oldest page only.
  const mkKey = (m) => `${m.kind}:${m.id || `${m.t}:${m.label}`}`;
  const uniq = (ms) => { const seen = new Set(); return ms.filter((m) => !seen.has(mkKey(m)) && seen.add(mkKey(m))); };
  function withHistory(c) {
    const h = D.hist;
    if (!h?.bars.length) return;
    const cut = c.bars[0]?.time ?? Infinity;
    c.bars = [...h.bars.filter((b) => b.time < cut), ...c.bars];
    c.markers = uniq([...h.markers, ...c.markers.filter((m) => !m.detail?.earlier)]);
  }
  async function loadOlder() {
    const c = D.candles, h = D.hist;
    if (!c || !h || h.key !== c.key || h.busy || h.done || !c.bars.length || D.mode !== "deep") return;
    const first = c.bars[0].time, [chain, address] = splitKey(D.key), tf = D.tf;
    h.busy = true;
    try {
      const o = normDeep(await get(`/api/deep/candles/${enc(chain)}/${enc(address)}?tf=${tf}&minutes=${ASK[tf]}&before=${
        encodeURIComponent(new Date(first * 1000).toISOString())}`), tf);
      if (D.candles !== c || D.hist !== h) return;
      const add = o.bars.filter((b) => b.time < first);
      if (!add.length) { h.done = true; note(); return; }
      h.bars = [...add, ...h.bars];
      h.markers = uniq([...o.markers, ...h.markers.filter((m) => !m.detail?.earlier)]);
      c.bars = [...add, ...c.bars];
      c.markers = uniq([...o.markers, ...c.markers.filter((m) => !m.detail?.earlier)]);
      c.byTime = new Map(c.bars.map((b) => [b.time, b]));
      const ts = D.chart?.chart.timeScale(), r = ts?.getVisibleLogicalRange();
      await drawCandles();
      if (ts && r) ts.setVisibleLogicalRange({ from: r.from + add.length, to: r.to + add.length });   // the view stays put
    } catch { /* the next time the view reaches the edge it tries again */ }
    finally { h.busy = false; }
  }

  // Each marker lands on the bar it happened in (or the last bar before it). A call names its group the first time that
  // group appears, and again once the last named call is a dozen bars back, so names never pile into each other.
  function placeMarkers(c) {
    const times = c.bars.map((b) => b.time), step = TF_MIN[D.tf] * 60, at = new Map(), out = [], named = new Set();
    let lastNamed = -Infinity, botNamed = false;
    const snap = (t) => {
      const s = Math.floor(t / 1000 / step) * step;
      if (!times.length || s < times[0] - step) return -1;
      let lo = 0, hi = times.length - 1;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (times[mid] <= s) lo = mid; else hi = mid - 1; }
      return lo;
    };
    for (const m of [...c.markers].sort((a, b) => a.t - b.t)) {
      const i = snap(m.t);
      if (i < 0) continue;
      const time = times[i];
      if (!at.has(time)) at.set(time, []);
      at.get(time).push(m);
      if (m.kind === "call") {
        const name = !named.has(m.label) || i - lastNamed >= 12;
        if (name) { named.add(m.label); lastNamed = i; }
        out.push({ time, position: "aboveBar", shape: "arrowDown", color: "#e7d9bf", text: name ? String(m.label).slice(0, 18) : "" });
      } else if (m.kind === "bot") {
        out.push({ time, position: "belowBar", shape: "arrowUp", color: "#86b6e6", text: botNamed ? "" : "bot" });
        botNamed = true;
      } else if (m.kind === "x") out.push({ time, position: "aboveBar", shape: "circle", color: "#d9a441", text: "X" });
    }
    c.at = at;
    return out.sort((a, b) => a.time - b.time);
  }

  async function drawCandles() {
    const c = D.candles;
    if (!c) return;
    if (c.bars.length < 2) {
      if (D.chart) { D.chart.candles.setData([]); D.chart.vol.setData([]); D.chart.candles.setMarkers([]); }
      $("#dz-legend").innerHTML = "";
      renderKinds(); layoutMarks();
      chartMsg(c.bars.length ? "One traded bar so far on this timeframe." : "No trades on this timeframe yet.");
      note();
      return;
    }
    let ch;
    try { ch = await ensureChart(); } catch (e) { return lineFallback(c, e); }
    if (c !== D.candles) return;
    $("#dz-chart-alt").hidden = true;
    $("#dz-chart").hidden = false;
    chartMsg(null);
    const lo = Math.min(...c.bars.map((b) => b.low)), hi = Math.max(...c.bars.map((b) => b.high));
    // four significant digits at the smallest price on screen
    ch.candles.applyOptions({ priceFormat: { type: "custom", formatter: price, minMove: 10 ** (Math.floor(Math.log10(lo)) - 3) } });
    const grey = "rgba(189, 180, 165, 0.6)";
    ch.candles.setData(c.bars.map(({ time, open, high, low, close, quote }) => (quote
      ? { time, open, high, low, close, color: close >= open ? "rgba(0, 0, 0, 0)" : grey, borderColor: grey, wickColor: grey }
      : { time, open, high, low, close })));
    ch.vol.setData(c.bars.map((b) => ({ time: b.time, value: b.volume || 0,
      color: b.close >= b.open ? "rgba(74, 136, 200, 0.42)" : "rgba(192, 69, 59, 0.42)" })));
    placeMarkers(c);                       // the legend's events per bar; the bubbles over the chart draw them (layoutMarks)
    ch.candles.setMarkers([]);
    ch.lines.forEach((l) => ch.candles.removePriceLine(l));
    ch.lines = [];
    const cp = D.data?.call?.first_price;
    if (cp > 0) ch.lines.push(ch.candles.createPriceLine({ price: cp, color: "#e7d9bf", lineWidth: 1, lineStyle: ch.L.LineStyle.Dashed, axisLabelVisible: true, title: "first call" }));
    if (D.log == null) D.log = hi / lo > 5;      // a wide range reads better in percent
    applyLog();
    // First draw of this token and timeframe: the latest bars at a readable width (the rest is a scroll or pinch away);
    // later refreshes keep wherever the reader has zoomed.
    if (D.fitKey !== `${D.key}|${D.tf}`) {
      const n = c.bars.length, show = Math.max(60, Math.floor(($("#dz-chart").clientWidth || 700) / 7));
      if (n > show) ch.chart.timeScale().setVisibleLogicalRange({ from: n - show, to: n + 2 });
      else ch.chart.timeScale().fitContent();
      D.fitKey = `${D.key}|${D.tf}`;
    }
    legend(null);
    note();
    renderKinds();
    relayMarks();
  }

  function applyLog() {
    syncChartControls();
    const ch = D.chart;
    if (ch) ch.chart.priceScale("right").applyOptions({ mode: D.log ? ch.L.PriceScaleMode.Logarithmic : ch.L.PriceScaleMode.Normal });
    relayMarks();
  }

  // Without the chart library (unpkg blocked or offline): the closes as a line, with the call price, so the page still reads.
  function lineFallback(c, e) {
    const alt = $("#dz-chart-alt");
    $("#dz-chart").hidden = true;
    alt.hidden = false;
    chartMsg(null);
    renderKinds(); layoutMarks();          // no candle chart, no bubbles
    const cp = D.data?.call?.first_price;
    drawChart(alt, c.bars.map((b) => ({ t: new Date(b.time * 1000), v: b.close })), cp ? [{ v: cp, label: "first call" }] : [],
      { fmt: price, label: "Bar", aria: `${D.tf} closes` });
    $("#dz-chart-note").innerHTML = `<span class="dz-note-warn">The candle chart couldn't load (${esc(e.message)}): closes as a line instead.</span>
      <button type="button" class="linkish dz-chart-retry">Try the candles again</button>`;
  }

  const GLYPH = { call: "▼", bot: "▲", bot_sale: "▼", x: "●" };
  function legend(time) {
    const c = D.candles;
    if (!c?.bars.length) return;
    const b = (time != null && c.byTime.get(time)) || c.bars[c.bars.length - 1];
    const chg = b.open ? ((b.close / b.open) - 1) * 100 : null;
    const all = c.at?.get(b.time) || [];
    const ev = all.slice(0, 3).map((m) => `<span class="lg-ev k-${esc(m.kind)}">${GLYPH[m.kind] || "·"} ${esc(m.label)}${m.who ? ` (${esc(m.who)})` : ""}${
      m.price ? ` at ${price(m.price)}` : ""}</span>`).concat(all.length > 3 ? [`<span class="lg-ev">+${all.length - 3} more on this bar</span>`] : []);
    $("#dz-legend").innerHTML = `<span class="lg-t">${new Date(b.time * 1000).toISOString().slice(5, 16).replace("T", " ")} UTC</span>
      <span>O <b>${price(b.open)}</b></span><span>H <b>${price(b.high)}</b></span><span>L <b>${price(b.low)}</b></span><span>C <b>${price(b.close)}</b></span>
      ${chg != null ? `<span class="lg-chg ${upDown(chg)}">${chg >= 0 ? "+" : "−"}${Math.abs(chg).toFixed(1)}%</span>` : ""}<span>Vol <b>${usd(b.volume)}</b></span>${ev.join("")}`;
  }

  function note() {
    const c = D.candles, el = $("#dz-chart-note");
    if (!c || !el || $("#dz-chart-alt").hidden === false) return;
    const src = { pipeline: "the price pipeline (GeckoTerminal candles, then our own live quotes)", albatross: "our own candles and live quotes",
      birdeye: "Birdeye" }[c.source] || esc(c.source || "the evidence engine");
    const q = c.bars.find((b) => b.quote);
    const behind = q ? ` Grey bars from ${hhmm(q.time)} UTC are built from our live quotes (the pool's own candles haven't been fetched that far), so they carry no volume.` : "";
    const key = [chartEvents().length ? "Bubbles mark what happened and when: ✈ a group call, a letter an X post by that account (blue ring "
      + "bullish, red bearish, dashed a shill or bot), gold a FOMO trader, ! a red flag or an X warning, ✓ a red flag cleared, ▲ a bot's buy and ▼ its sale. "
      + "Hover or tap one for the detail; the buttons above the chart turn each kind off" : "",
      D.data?.call?.first_price ? "The dashed line is the first call's price" : ""].filter(Boolean).join(". ");
    el.innerHTML = `${c.bars.length} ${D.tf} bars from ${src}.${behind} Hollow candles closed up, filled closed down.${key ? ` ${key}.` : ""}${
      D.hist?.key === c.key && D.mode === "deep" ? (D.hist.done ? " That's as far back as our candles go." : " Scroll left for earlier candles.") : ""}
      <a href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noopener noreferrer">Chart: TradingView Lightweight Charts</a>.
      ${D.mode === "fallback" || D.data?.fallback ? `<button type="button" class="linkish dz-pricecheck">How this price is checked</button>` : ""}`;
  }

  // ─── dossier wiring ──────────────────────────────────────────────────────
  retries.set("dossier", () => { ENGINE.downAt = 0; loadDossier(); });
  $("#dz-tf").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tf]");
    if (!b || b.dataset.tf === D.tf) return;
    D.tf = D.tfPref = b.dataset.tf;
    try { localStorage.setItem("albatross.deep.tf", D.tf); } catch { /* remembered for this visit only */ }
    D.candles = null;
    syncChartControls();
    chartMsg("Loading the candles…");
    loadCandles();
  });
  $("#dz-log").addEventListener("click", () => { D.log = !D.log; applyLog(); });
  dossTab.addEventListener("toggle", (e) => {
    const s = e.target.closest?.(".dz-sec");
    if (s) D.open.set(s.dataset.k, s.open);
  }, true);
  const goSection = (k) => {
    const el = k === "market" ? $("#dz-stats") : $(`#dz-sec-${k}`);
    if (!el) return;
    if (el.tagName === "DETAILS") { el.open = true; D.open.set(k, true); }
    el.scrollIntoView({ behavior: CALM.matches ? "auto" : "smooth", block: "start" });
    el.classList.remove("dz-flash"); void el.offsetWidth; el.classList.add("dz-flash");
  };
  dossTab.addEventListener("click", (e) => {
    const li = e.target.closest(".dz-ck[data-sec]");
    if (li) { goSection(li.dataset.sec); return; }
    const c = e.target.closest(".dz-copy");
    if (c) {
      const done = (ok) => { c.textContent = ok ? "Copied ✓" : "Copy failed"; c.classList.toggle("ok", ok); setTimeout(() => { c.textContent = "Copy CA"; c.classList.remove("ok"); }, 1600); };
      if (navigator.clipboard?.writeText) navigator.clipboard.writeText(c.dataset.copy).then(() => done(true), () => done(false));
      else {
        const ta = Object.assign(document.createElement("textarea"), { value: c.dataset.copy });
        document.body.append(ta); ta.select();
        let ok = false;
        try { ok = document.execCommand("copy"); } catch { /* the address stays selectable by hand */ }
        ta.remove(); done(ok);
      }
      return;
    }
    if (e.target.closest(".dz-chart-retry")) { lwLoading = null; drawCandles(); return; }
    const kind = e.target.closest("[data-mk-kind]");
    if (kind) {
      const g = kind.dataset.mkKind;
      if (mkOff.has(g)) mkOff.delete(g); else mkOff.add(g);
      try { localStorage.setItem("albatross.marksOff", JSON.stringify([...mkOff])); } catch { /* the choice lasts this visit */ }
      renderKinds(); layoutMarks();
      return;
    }
    if (e.target.closest("[data-xask]:not([disabled])")) { askX(); return; }
    if (e.target.closest(".dz-pricecheck") && window.microOpen) window.microOpen(D.key);
  });
  dossTab.addEventListener("keydown", (e) => {
    const li = e.target.closest(".dz-ck[data-sec]");
    if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); goSection(li.dataset.sec); }
  });

  // ─── loading cadence ─────────────────────────────────────────────────────
  document.addEventListener("viewchange", (e) => {
    const v = e.detail.view;
    if (v === "deep") {
      renderFeed();
      if (F.scroll) { const y = F.scroll; F.scroll = 0; requestAnimationFrame(() => scrollTo({ top: y })); }   // Back lands where the reader left the feed
    }
    if (v === "dossier") showDossier();
  });
  document.addEventListener("refresh", (e) => { if (e.detail.view === "deep") loadFeed(); });   // app.js: navigation and its minute tick
  setInterval(() => {
    if (document.hidden) return;
    if (state.tab === "deep") loadFeed();
    else if (state.tab === "dossier") loadDossier();          // it reloads the candles too once they are 15 s old
  }, 20_000);
  // Back on the tab: the refresh above skipped the hidden time, so a dossier catches up at once (app.js refreshes the
  // feed after a minute away)
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.tab === "dossier") loadDossier(); });
  // Opened straight onto the Deep End: app.js routed before this script loaded, so draw the view now.
  if (state.tab === "deep") { renderFeed(); loadFeed(); }
  if (state.tab === "dossier") showDossier();
})();
