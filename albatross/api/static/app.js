"use strict";

const TFS = ["5m", "15m", "1h", "4h", "1d", "1w"];
const state = {
  tiers: new Set([1, 2]), cat: "", q: "", impact: 0, sent: "", newsAsset: "", matrix: null, sectors: null,
  socialKind: "asset", socialDays: 7, social: null, scSource: "quant", scDays: 0, sc: null,
  alerts: null,
  matrixSource: null,            // null until the server picks: Strategist once it has calls, else quant
  ideaStatus: "", ideaSource: "", ideas: null, openIdeas: new Set(), reportId: null, reportKind: "", newsLimit: 60,
  iscPolicy: "stop_target", iscSplit: "total", isc: null, iscTotal: null, scLb: null,
  tab: "home", coin: null, coinFrom: null, matrixMore: false,
  deep: null, deepFrom: null,     // the Deep End dossier open now ({chain, address, label}) and the view it was opened from
};
const tips = new Map();          // free-form tooltip bodies, keyed by td[data-tipid]
let tipSeq = 0;
const tipId = (html) => { const id = `t${++tipSeq}`; tips.set(id, html); return id; };
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ─── formatting ────────────────────────────────────────────────────────────
function fmtPrice(p) {
  if (p == null) return "–";
  const a = Math.abs(p);
  // Sub-cent coins keep four significant digits (0.000004215, not 0.0000042).
  if (a && a < 0.01) return p.toLocaleString(undefined, { maximumSignificantDigits: 4 });
  const d = a >= 1000 ? 0 : a >= 1 ? 2 : 4;
  return p.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
const fmtPct = (v, d = 1) => (v == null || !isFinite(v) ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
const fmtUsd = (v) => (v == null ? "–" : v >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`);
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, (Date.now() - new Date(ts)) / 1000);   // server and browser clocks differ by a second or two
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
}
const utc = (ts) => (ts ? new Date(ts).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "–");

// ─── tooltip ───────────────────────────────────────────────────────────────
const tip = $("#tip");
function showTip(e, html) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const pad = 14, r = tip.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => (tip.style.display = "none");
const row = (k, v) => `<div class="row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;

// ─── bias cells ────────────────────────────────────────────────────────────
// key: matrix lookup for sigTip; tipHtml: a ready-made tooltip body instead.
function cellHtml(sig, key, tipHtml) {
  if (!sig) return `<td class="cell empty"><span>·</span></td>`;
  const stale = new Date(sig.valid_until) < Date.now();
  const s = sig.score ?? sig.bias / 2;
  const pole = s < 0 ? "var(--bear)" : "var(--bull)";
  const mag = Math.min(1, Math.abs(s)).toFixed(2);
  const label = sig.bias > 0 ? `▲${sig.bias}` : sig.bias < 0 ? `▼${-sig.bias}` : "0";
  const tipAttr = tipHtml ? ` data-tipid="${tipId(tipHtml)}"` : key ? ` data-tip="${esc(key)}"` : "";
  return `<td class="cell${stale ? " stale" : ""}"${tipAttr}><span style="--pole:${pole};--mag:${mag}">${label}</span></td>`;
}

const SOURCE_LABEL = {
  quant: "Quant", quant_replay: "Replay (walk-forward)", ensemble: "Blended call", strategist: "Strategist",
  ltf_analyst: "LTF analyst", htf_analyst: "HTF analyst", flow_analyst: "Flow analyst", social_pulse: "X (Social Pulse)", macro_analyst: "Macro analyst", jev_analyst: "Jev (TypeSafe)",
};
const humanize = (s) => (s ? s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()) : "–");
// New agents read as words until they get a label; free-form ids (callers, rules) stay as logged.
const srcLabel = (s) => SOURCE_LABEL[s] || (/^strategist@/.test(s ?? "") ? `Strategist on ${s.slice(11)} (trial)` : null) || (/^[a-z][a-z0-9_]*$/.test(s ?? "") ? humanize(s) : s);
const signed = (v, d = 2) => (v == null ? "–" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}`);

// Thesis / rationale and inputs an agent stored with its call, for tooltips.
function whyHtml(s) {
  const parts = s.parts ? Object.entries(s.parts).sort((a, b) => b[1].w - a[1].w)
    .map(([src, p]) => row(srcLabel(src), `${signed(p.score)} × ${p.w.toFixed(2)}`)).join("") : "";
  return (parts ? `<div class="tip-h">Blend (score × weight)</div>${parts}` : "") +
    (s.thesis ? `<div class="tip-text">${esc(s.thesis)}</div>` : "") +
    (s.relies_on?.length ? `<div class="tip-text cat">Relies on ${esc(s.relies_on.map(srcLabel).join(", "))}</div>` : "");
}

function regime(tfs) {
  const avg = (ks) => {
    const v = ks.map((k) => tfs[k]?.score).filter((x) => x != null);
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  };
  const ltf = avg(["5m", "15m", "1h"]), htf = avg(["1d", "1w"]);
  if (ltf == null || htf == null) return "–";
  if (htf > 0.2 && ltf < -0.2) return "Pullback in uptrend";
  if (htf < -0.2 && ltf > 0.2) return "Bounce in downtrend";
  if (htf > 0.2 && ltf > 0.2) return "Aligned up";
  if (htf < -0.2 && ltf < -0.2) return "Aligned down";
  if (htf > 0.2) return "Uptrend, short term flat";
  if (htf < -0.2) return "Downtrend, short term flat";
  return "Range / mixed";
}

// ─── data ──────────────────────────────────────────────────────────────────
// Identical requests in flight share one answer, so the minute refresh never stacks on a slow response.
const inflight = new Map();
const TIMEOUT_S = 30;
const net = { down: false, okAt: null };     // okAt: the last time the server answered at all

function get(url) {
  if (!inflight.has(url)) inflight.set(url, fetchJson(url).finally(() => inflight.delete(url)));
  return inflight.get(url);
}

async function fetchJson(url) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), TIMEOUT_S * 1000);
  try {
    const r = await fetch(url, { signal: ctl.signal });
    // 502-504 come from the proxy in front: the app behind it isn't answering.
    netState(r.status >= 502 && r.status <= 504);
    // Signed out (the 30-day cookie ran out, or the key was rotated): back to the login page, then here.
    if (r.status === 401) location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search + location.hash)}`);
    if (!r.ok) throw Object.assign(new Error(`${url}: ${r.status}`), { status: r.status });
    return await r.json();
  } catch (e) {
    if (e.status || e instanceof SyntaxError) throw e;
    const err = Object.assign(new Error(`${url}: ${e.message}`), { kind: e.name === "AbortError" ? "timeout" : "network" });
    netState(true, err);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// The reason a load failed, in words.
function whyFailed(e) {
  if (e.kind === "timeout") return `the server took more than ${TIMEOUT_S} seconds to answer`;
  if (e.kind === "network") return navigator.onLine === false ? "this device is offline" : "the server can't be reached";
  if (e.status >= 502 && e.status <= 504) return `the server isn't answering (HTTP ${e.status})`;
  if (e.status === 401 || e.status === 403) return `access was refused (HTTP ${e.status}); reload the page to sign in again`;
  if (e.status === 404) return "the server doesn't have it (HTTP 404)";
  if (e.status) return `the server hit an error (HTTP ${e.status})`;
  if (e instanceof SyntaxError) return "the server's answer couldn't be read";
  return "this page couldn't display what came back";        // a rendering bug: the console has the details
}
const offline = (e) => e.kind || (e.status >= 502 && e.status <= 504);
const hhmm = (t) => `${new Date(t).toISOString().slice(11, 16)} UTC`;

// ─── load states ───────────────────────────────────────────────────────────
// Every loader runs through guarded(what, hosts, fn, key): an empty panel says "Loading…" while it waits; on
// failure it says what couldn't load, why, and offers a retry. A panel that already shows data keeps it, and
// the view gets one line saying the data is from an earlier time. `key` names what the data depends on (a
// filter, a coin): after it changes, the old data is the wrong data, not stale data, so it is replaced.
// If the server can't be reached at all, one toast says so for every view. A loader never rejects.
const retries = new Map();
let retrySeq = 0;

function guarded(what, hosts, fn, key = () => "") {
  const id = `r${++retrySeq}`;
  let okAt = null, okKey = null, lastArgs = [];
  const run = async (...args) => {
    lastArgs = args;
    const k = JSON.stringify([args, key()]);
    const els = hosts.map((s) => $(s)).filter(Boolean);
    const showing = (el) => !!el.textContent.trim() && !el.querySelector(".load-state");
    els.forEach((el, i) => { if (!el.textContent.trim()) placeholder(el, "loading", i ? "Loading…" : `Loading ${what}…`); });
    const slow = setTimeout(() => els.forEach((el) => el.querySelector(".load-state.loading .msg")
      ?.replaceChildren(`Still waiting for ${what}: the server is slow right now.`)), 8000);
    try {
      const out = await fn(...args);
      okAt = Date.now(); okKey = JSON.stringify([args, key()]);   // a loader may settle its own key (the default source)
      run.error = null;
      els.forEach((el) => { if (el.querySelector(".load-state")) el.innerHTML = ""; });   // a host the loader left alone
      document.querySelector(`.load-note[data-retry="${id}"]`)?.remove();
      return out;
    } catch (e) {
      if (!e.status && !e.kind) console.error(e);
      const why = run.error = whyFailed(e), current = k === okKey;
      let lead = true;
      els.forEach((el) => {
        if (current && showing(el)) return;
        placeholder(el, "failed", lead
          ? `<b>Couldn't load ${esc(what)}.</b> <span class="msg">${esc(cap(why))}. Retrying automatically.</span>`
          : `<span class="msg">Unavailable: waiting on ${esc(what)}.</span>`, lead && id);
        lead = false;
      });
      const shown = current && els.some(showing);
      const tab = els[0]?.closest(".tab");
      let note = document.querySelector(`.load-note[data-retry="${id}"]`);
      if (shown && tab && !offline(e)) {       // offline: the toast already says the page is frozen
        note ??= tab.insertAdjacentElement("afterbegin", Object.assign(document.createElement("div"), { className: "load-note" }));
        note.dataset.retry = id;
        note.setAttribute("role", "status");
        note.innerHTML = `<span><b>Couldn't refresh ${esc(what)}:</b> ${esc(why)}. Showing what loaded at ${hhmm(okAt)} (${ago(okAt)}); retrying automatically.</span>
          <button class="load-retry" data-retry="${id}">Try now</button>`;
      } else note?.remove();
      if (!els.length && !offline(e)) toast(`<b>Couldn't open ${esc(what)}:</b> ${esc(why)}.`);
    } finally {
      clearTimeout(slow);
    }
  };
  retries.set(id, () => run(...lastArgs));
  return run;
}

function placeholder(el, kind, html, retryId) {
  const box = `<div class="load-state ${kind}" role="status">${kind === "loading" ? `<span class="msg">${html}</span>` : html}${
    retryId ? ` <button class="load-retry" data-retry="${retryId}">Try now</button>` : ""}</div>`;
  el.innerHTML = el.tagName === "TABLE" ? `<tbody><tr><td class="load-cell">${box}</td></tr></tbody>`
    : /^[OU]L$/.test(el.tagName) ? `<li class="load-cell">${box}</li>` : box;
}

document.addEventListener("click", (e) => {
  const b = e.target.closest(".load-retry");
  if (!b) return;
  b.disabled = true; b.textContent = "Trying…";
  if (b.dataset.retry === "net") refresh(); else retries.get(b.dataset.retry)?.();
});

// One toast for the whole page: sticky while the server can't be reached, otherwise gone after 8 s.
const toastEl = Object.assign(document.createElement("div"), { className: "load-toast", hidden: true });
toastEl.setAttribute("role", "alert");
document.body.append(toastEl);
let toastTimer;
function toast(html, sticky = false) {
  clearTimeout(toastTimer);
  toastEl.innerHTML = html;
  toastEl.hidden = false;
  toastEl.classList.toggle("down", sticky);
  if (!sticky) toastTimer = setTimeout(() => (toastEl.hidden = true), 8000);
}

function netState(down, err) {
  if (!down) {
    net.okAt = Date.now();
    if (net.down) { net.down = false; toastEl.hidden = true; }
    return;
  }
  net.down = true;
  toast(`<span><b>Can't reach Albatross:</b> ${esc(err ? whyFailed(err) : "the server isn't answering")}.
    ${net.okAt ? `Everything on screen is from ${hhmm(net.okAt)} (${ago(net.okAt)}).` : "Nothing has loaded yet."} Retrying every minute.</span>
    <button class="load-retry" data-retry="net">Try now</button>`, true);
}

const MATRIX_NOTE = {
  quant: "Quant baseline: deterministic score from trend, structure, momentum and slope. Regime is derived from the scores.",
  ensemble: "Blended call: every live source blended by its measured edge over a one-way (always-long or always-short) caller (weights on the asset view). Hover for the blend; confidence is agreement, the share of weight on the winning side.",
  strategist: "Strategist: the final call per timeframe, at most one step from the blended call, with its rationale on hover. Regime is the Strategist's own label (4h, else 1h).",
};

async function loadMatrix() {
  const q = new URLSearchParams({ tiers: "1,2,3" });
  if (state.matrixSource) q.set("source", state.matrixSource);
  state.matrix = await get(`/api/matrix?${q}`);
  state.matrixSource = state.matrix.source;
  if (state.cat) renderSubnav();          // a sector's crumb was drawn before its name arrived ("Defi" for DeFi)
  document.querySelectorAll("#src-chips button").forEach((b) => {
    const has = state.matrix.available.includes(b.dataset.src);
    b.classList.toggle("on", b.dataset.src === state.matrix.source);
    b.classList.toggle("dim", !has);
    b.title = has ? "" : "No calls from this source in the last 30 days";
  });
  $("#matrix-note").textContent = `${MATRIX_NOTE[state.matrix.source]} ${benchedNote(state.matrix)}${uncoveredNote(state.matrix.uncovered)}The 24h move is the Hyperliquid mark where we track one, else the venue's hourly candles. ` +
    "A coin sits in every sector it is tagged with. Hover a cell for details; click a row for the coin page.";
  // Every sector with a coin in it, with the count its drill-down will list.
  const count = new Map();
  for (const a of state.matrix.assets) for (const c of new Set([a.primary_category, ...(a.categories || [])])) if (c) count.set(c, (count.get(c) || 0) + 1);
  const sel = $("#cat-filter"), cur = state.cat;
  sel.innerHTML = `<option value="">All sectors</option>` + [...count].map(([slug, n]) => [slug, catName(slug), n]).sort((a, b) => a[1].localeCompare(b[1]))
    .map(([slug, name, n]) => `<option value="${esc(slug)}" ${slug === cur ? "selected" : ""}>${esc(name)} (${n})</option>`).join("");
  renderMatrix();
}

// Timeframes no live source covers (every source of the ensemble culled): one sentence, and a "–" cell with the reason.
function uncoveredNote(unc) {
  const tfs = Object.keys(unc || {});
  if (!tfs.length) return "";
  return `No source with skill covers ${tfs.join(" or ")}: ${Object.values(unc)[0]}, so neither the blended call nor the Strategist calls ${tfs.length > 1 ? "them" : "it"}. `;
}
// A source the culling system has benched, picked by hand: its calls show for reference and say so.
const BENCHED = { probation: "benched", paused: "paused", retired: "retired" };
function benchedNote(mx) {
  return mx?.source_rung ? `The ${srcLabel(mx.source).toLowerCase()} is ${BENCHED[mx.source_rung]} (out of the blended call): its calls are shown for reference. ` : "";
}
const noSkillCell = (tf, why) => `<td class="cell empty unc" title="${esc(`No source with skill at ${tf}: ${why}.`)}"><span>–</span></td>`;

// Phones fold the 24h move under the price instead of giving it a column.
// A move that rounds to 0.00% is flat: no arrow, no colour.
const moveTone = (v) => (+v.toFixed(2) > 0 ? "up" : +v.toFixed(2) < 0 ? "down" : "flat");
const ARROW = { up: "▲", down: "▼", flat: "" };
const pxMove = (v) => (v == null ? "" : `<span class="px-move ${moveTone(v)}">${ARROW[moveTone(v)]}${Math.abs(v).toFixed(2)}%</span>`);
const deltaCell = (v) => (v == null ? "–" : `<span class="delta ${moveTone(v)}">${ARROW[moveTone(v)]} ${Math.abs(v).toFixed(2)}%</span>`);

function renderMatrix() {
  if (!state.matrix) return;
  const q = state.q.trim().toUpperCase();
  const inSector = (a) => !state.cat || a.primary_category === state.cat || (a.categories || []).includes(state.cat);
  // A sector lists every tier; the tier chips narrow All coins; a search finds any coin by symbol or name.
  const found = (a) => a.symbol.includes(q) || (a.name || "").toUpperCase().includes(q);
  const rows = state.matrix.assets.filter((a) => inSector(a) && (q ? found(a) : state.cat || state.tiers.has(a.tier)));
  $("#tier-chips").hidden = !!state.cat;
  // Default view: price, the day's move and the three timeframes that matter most. "More" adds the rest.
  const more = state.matrixMore, tfs = more ? TFS : ["1h", "4h", "1d"], unc = state.matrix.uncovered || {};
  // Coins without a call on these timeframes are named under the table instead of filling it with dots.
  const called = (a) => tfs.some((t) => a.tfs[t]);
  const shown = q ? rows : rows.filter(called), rest = q ? [] : rows.filter((a) => !called(a));
  const head = `<thead><tr><th>Coin</th>${more ? `<th class="left">Sector</th>` : ""}<th>Price</th><th class="chg" title="Change over the last 24 hours">24h</th>${more ? `<th title="Hyperliquid funding, annualised. ~10.95% is its neutral baseline (the interest component), not long crowding">Funding APR</th><th>OI</th><th title="Open interest change over 24h">OI 24h</th>` : ""}${tfs.map((t) => (unc[t] ? `<th class="c unc" title="${esc(`No source with skill at ${t}: ${unc[t]}.`)}">${t}</th>` : `<th class="c">${t}</th>`)).join("")}<th class="left regime">Regime</th></tr></thead>`;
  const body = shown.map((a) => {
    const d = a.derivs || {};
    const price = a.price ?? a.tfs["1h"]?.ref_price ?? a.tfs["1d"]?.ref_price;
    const apr = d.funding_rate != null ? d.funding_rate * 24 * 365 * 100 : null;
    const oiChg = d.oi_usd && d.oi_usd_24h ? (d.oi_usd / d.oi_usd_24h - 1) * 100 : null;
    return `<tr data-sym="${esc(a.symbol)}" tabindex="0"><td><span class="sym">${esc(a.symbol)}</span> <span class="cat">T${a.tier}</span></td>
      ${more ? `<td class="cat left">${esc(a.category_name || a.primary_category)}</td>` : ""}<td class="px">${fmtPrice(price)}${pxMove(a.chg_24h_pct)}</td><td class="chg">${deltaCell(a.chg_24h_pct)}</td>
      ${more ? `<td>${fmtPct(apr, 1)}</td><td>${fmtUsd(d.oi_usd)}</td><td>${fmtPct(oiChg)}</td>` : ""}
      ${tfs.map((t) => (unc[t] ? noSkillCell(t, unc[t]) : cellHtml(a.tfs[t], `${a.symbol}|${t}`))).join("")}
      <td class="regime">${esc(assetRegime(a))}</td></tr>`;
  }).join("");
  const none = state.cat && !state.matrix.sector_names?.[state.cat] ? `There is no sector called “${esc(state.cat)}”.`
    : q ? `No coin matches “${esc(state.q.trim())}”${state.cat ? ` in ${esc(catName(state.cat))}` : ""}.` : "No coins in this view.";
  $("#matrix").innerHTML = head + `<tbody>${body || `<tr><td colspan="14" class="left">${none}</td></tr>`}</tbody>`;
  const src = srcLabel(state.matrix.source), restEl = $("#matrix-rest");
  const keepOpen = restEl.dataset.cat === state.cat && $("details", restEl)?.open;   // the minute refresh must not fold it shut
  restEl.dataset.cat = state.cat;
  restEl.innerHTML = rest.length ? `<details class="fold rest"${keepOpen ? " open" : ""}><summary>${rest.length} more coin${rest.length === 1 ? "" : "s"} with no ${esc(src)} call on ${tfs.length === 3 ? "1h, 4h or 1d" : "any timeframe"}</summary>
      <div class="rest-body"><p class="note">${state.matrix.source === "strategist" ? "The Strategist covers tier 1 and 2 coins with a live price. " : ""}Open a coin for whatever calls and chart it does have.</p>
      <div class="rest-list">${rest.map((a) => `<button type="button" data-sym="${esc(a.symbol)}" title="${esc(a.name || a.symbol)} · tier ${a.tier}">${esc(a.symbol)}</button>`).join("")}</div></div></details>` : "";
  document.dispatchEvent(new CustomEvent("matrixrendered", { detail: {
    n: state.cat ? state.matrix.assets.filter(inSector).length : rows.length, called: shown.length, rest: rest.length,
    source: state.matrix.source, cat: state.cat, searching: !!q } }));
}

async function loadSectors() {
  state.sectors = await get("/api/sectors");
  const head = `<thead><tr><th>Sector</th><th title="Coins tagged with the sector: what clicking it lists">Coins</th>${TFS.map((t) => `<th class="c">${t}</th>`).join("")}<th class="left">Regime</th></tr></thead>`;
  // Biggest first, like the treemap above it.
  const body = [...state.sectors.sectors].sort((a, b) => b.coins - a.coins).map((s) =>
    `<tr data-sector="${esc(s.slug)}" class="drill"><td class="sym">${esc(s.name)}</td><td>${s.coins}</td>${TFS.map((t) => cellHtml(s.tfs[t], `sector:${s.slug}|${t}`)).join("")}<td class="regime">${esc(regime(s.tfs))}</td></tr>`).join("");
  $("#sectors").innerHTML = head + `<tbody>${body || `<tr><td colspan="9">No sector signals yet.</td></tr>`}</tbody>`;
}

function lookupSig(key) {
  const [scope, tf] = key.split("|");
  if (scope.startsWith("sector:")) {
    const s = state.sectors?.sectors.find((x) => `sector:${x.slug}` === scope);
    return s && { scope: s.name, tf, sig: s.tfs[tf], sector: true };
  }
  const a = state.matrix?.assets.find((x) => x.symbol === scope);
  return a && { scope, tf, sig: a.tfs[tf] };
}

function sigTip(key) {
  const f = lookupSig(key);
  if (!f?.sig) return "";
  const s = f.sig, stale = new Date(s.valid_until) < Date.now();
  const dir = s.bias > 0 ? "Bullish" : s.bias < 0 ? "Bearish" : "Neutral";
  const src = f.sector ? "quant" : state.matrix?.source;
  return `<b>${esc(f.scope)} · ${esc(f.tf)}</b> — ${dir} ${s.bias > 0 ? "+" : ""}${s.bias}${stale ? " (stale)" : ""} <span class="cat">${esc(srcLabel(src))}</span>` +
    row("Score", (s.score ?? 0).toFixed(2)) +
    (f.sector ? row("Breadth", `${Math.round((s.breadth ?? 0) * 100)}%`)
      : row(src === "ensemble" ? "Agreement" : "Confidence", `${Math.round((s.confidence ?? 0) * 100)}%`) + row("Ref price", fmtPrice(s.ref_price)) +
        (s.invalidation != null ? row("Invalidation", fmtPrice(s.invalidation)) : "")) +
    row("Candle", utc(s.as_of)) + row("Valid until", utc(s.valid_until)) +
    (f.sector ? `<div class="tip-text">Volume-weighted mean of the quant scores of the ${s.members ?? "–"} coins whose main sector this is.</div>` : whyHtml(s));
}

// Strategist rows carry its own regime label; other sources get one derived from their scores.
const assetRegime = (a) => (state.matrix?.source === "strategist" ? humanize(a.regime) : regime(a.tfs));

const NEWS_PAGE = 60, NEWS_MAX = 300;          // the API serves at most 300
async function loadNews() {
  const q = new URLSearchParams({ limit: state.newsLimit, min_impact: state.impact });
  if (state.sent) q.set("sentiment", state.sent);
  const asset = state.newsAsset.trim().toUpperCase();
  if (asset) q.set("asset", asset);
  const items = await get(`/api/news?${q}`);
  let day = "";
  const filtered = state.impact || state.sent || asset;
  $("#news").innerHTML = items.map((n) => {
    const d = dayLabel(n.published_at), head = d === day ? "" : `<li class="nw-day">${esc(d)}</li>`;
    day = d;
    return head + newsItem(n);
  }).join("") || `<li class="nw-empty">${filtered
    ? `No headlines match${asset ? ` <b>${esc(asset)}</b> with` : ""} these filters. <button class="linkish" data-news-reset>Clear filters</button>`
    : "No headlines yet. The wire is polled every 3 minutes, and News Scout scores new headlines every 5."}</li>`;
  // Folded duplicates count toward the limit, so "more" is judged on the rows the API read.
  const read = items.reduce((a, n) => a + 1 + (n.also?.length || 0), 0);
  const more = $("#news-more");
  more.hidden = read < state.newsLimit || state.newsLimit >= NEWS_MAX;
  $("#news-end").textContent = read >= NEWS_MAX ? `The newest ${NEWS_MAX} headlines. Narrow it with the filters above.` : "";
}

// "Today", "Yesterday", else the weekday and date, in the reader's own time zone.
function dayLabel(ts) {
  const d = new Date(ts), t = new Date(), y = new Date(Date.now() - 864e5);
  return d.toDateString() === t.toDateString() ? "Today" : d.toDateString() === y.toDateString() ? "Yesterday"
    : d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

const NEWS_SOURCE = {
  coindesk: "CoinDesk", cointelegraph: "Cointelegraph", theblock: "The Block", decrypt: "Decrypt", bitcoinmagazine: "Bitcoin Magazine",
  cryptoslate: "CryptoSlate", blockworks: "Blockworks", dlnews: "DL News", cryptopanic: "CryptoPanic",
};
const NEWS_KIND = {
  etf_institutional: "ETF / institutional", hack_exploit: "Hack / exploit", partnership_adoption: "Partnership / adoption",
  onchain_flow: "On-chain flow", market_commentary: "Commentary", other: "",
};
// ─── GitHub guard ───
// Nothing hosted on GitHub is opened from this site. A GitHub link stays clickable only as a repo or
// account page (github.com/owner or github.com/owner/repo); a file, a download, a gist or a Pages site on GitHub is
// shown without a link, and no image is ever loaded from GitHub (deep.js logo()). Server side: albatross/github_guard.py.
const GITHUB_HOST = /(^|\.)(github\.com|githubusercontent\.com|github\.io|githubassets\.com|github\.dev|githubapp\.com|ghcr\.io)$/i;
function githubHosted(u) {
  try { return GITHUB_HOST.test(new URL(String(u)).hostname.replace(/\.$/, "")); } catch { return false; }
}
function githubPage(u) {
  try {
    const x = new URL(String(u));
    return /^(www\.)?github\.com$/i.test(x.hostname) && !x.search && !x.hash && !x.username && !x.password && x.port === ""
      && /^\/[A-Za-z0-9-]+(\/[A-Za-z0-9._-]+)?\/?$/.test(x.pathname) && !/\/\.\.?\/?$/.test(x.pathname);
  } catch { return false; }
}
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") && (!githubHosted(u) || githubPage(u)) ? u : null);
// ─── end GitHub guard ───
// Tone and impact are spelled out; the coloured left edge only repeats the tone. Asset chips filter the wire.
function newsItem(n) {
  const c = n.classification || {}, s = n.sentiment, url = safeUrl(n.url);
  const tone = s == null ? "" : s > 0 ? `<span class="tone bull">▲ ${s > 1 ? "Strongly bullish" : "Bullish"}</span>`
    : s < 0 ? `<span class="tone bear">▼ ${s < -1 ? "Strongly bearish" : "Bearish"}</span>` : `<span class="tone">Neutral</span>`;
  const imp = n.impact ? `<span class="imp${n.impact >= 4 ? " hi" : ""}" title="News Scout's impact score: 1 is noise, 5 moves the market">Impact ${n.impact}/5</span>`
    : `<span class="imp">Not scored yet</span>`;
  const assets = (n.assets || []).slice(0, 6).map((a) =>
    a === "MARKET" ? `<a class="asset mkt" href="#/news" data-news-asset="MARKET" title="Every market-wide headline">Market-wide</a>`
      : `<a class="asset" href="#/news" data-news-asset="${esc(a)}" title="Every headline tagged ${esc(a)}">${esc(a)}</a>`).join("");
  const kind = !c.kind ? "" : c.kind in NEWS_KIND ? NEWS_KIND[c.kind] : humanize(c.kind);
  const others = (n.also || []).filter((x) => x.source !== n.source && safeUrl(x.url));
  const meta = [esc(NEWS_SOURCE[n.source] || n.source), esc(kind), c.horizon ? `Horizon: ${esc(c.horizon)}` : "",
    others.length ? `also ${others.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(NEWS_SOURCE[x.source] || x.source)}</a>`).join(", ")}` : ""]
    .filter(Boolean).join(" · ");
  return `<li class="nw${s > 0 ? " bull" : s < 0 ? " bear" : ""}"><time class="when" datetime="${esc(n.published_at)}" title="${utc(n.published_at)}">${ago(n.published_at)}</time>
    <div class="nw-b">${url ? `<a class="nw-t" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : `<span class="nw-t">${esc(n.title)}</span>`}
      <div class="nw-tags">${tone}${imp}${assets}<span class="nw-meta">${meta}</span></div>
      ${c.reason ? `<p class="nw-why">${esc(c.reason)}</p>` : ""}</div></li>`;
}

// ─── system health ─────────────────────────────────────────────────────────
// The Health view answers "is everything OK?" in one card (from the alerts and the job states), with the
// detail folded underneath: every job, today's agent spend, resolved alerts, data collected.
const VENUE = { hl: "Hyperliquid", okx: "OKX", cb: "Coinbase" };
const CANDLE_TF = { "5m": "5-minute", "1h": "hourly", "1d": "daily" };
const JOB_AREAS = ["Market data", "News and X", "Signals and agents", "Grading and reports", "Micro-caps", "Alerts", "The Deep End"];
const JOB = {   // job id -> [area index, what it does]
  universe: [0, "Coin list and tiers"], derivs_hl: [0, "Hyperliquid funding and open interest"],
  derivs_cex: [0, "Binance / Bybit funding and open interest (Coinalyze)"], fear_greed: [0, "Fear & Greed index"],
  fear_greed_backfill: [0, "Fear & Greed history backfill"], macro_data: [0, "Macro data: rates, ETF flows, liquidity"],
  news: [1, "News feeds"], news_scout: [1, "News Scout: scores each headline"],
  social_pulse: [1, "X sentiment: hourly market pulse and coin reads"],
  features_ltf: [2, "Indicators, 5m and 15m"], features_htf: [2, "Indicators, 1h and 4h"], features_daily: [2, "Indicators, daily and weekly"],
  ltf_analyst: [2, "LTF analyst"], htf_analyst: [2, "HTF analyst"], flow_analyst: [2, "Flow analyst"], macro_analyst: [2, "Macro analyst"],
  ensemble: [2, "Blended call: blends every source"], strategist: [2, "Strategist: final calls and ideas"],
  oracle_watch: [2, "Oracle: escalates big moves"], oracle_daily: [2, "Oracle daily thesis"], oracle_weekly: [2, "Oracle weekly review"],
  ceo: [2, "CEO memo"], codex_limits: [3, "ChatGPT allowance reading"],
  strategy_lab: [3, "Strategy lab: tests new strategies"], alpha_report: [3, "Alpha: loss avoidance and factor edge"], alpha_live: [3, "Alpha: live verdicts on every call"], strategy_researcher: [2, "Strategy researcher: proposes strategies"],
  grader: [3, "Grades every signal against price"], grade_ideas: [3, "Backtests every idea"], regrade_ideas: [3, "Re-grades every idea"],
  replay: [3, "Walk-forward replay"], ledger_verify: [3, "Ledger hash-chain check"],
  reports_brief: [3, "4-hour brief"], reports_daily: [3, "Daily wrap"],
  micro_snapshots: [4, "Prices"], micro_pools: [4, "Pool discovery"], micro_candles: [4, "1-minute candles"],
  micro_reconcile: [4, "Cross-source price check"], micro_retire: [4, "Retires dead tokens"], micro_ledger: [4, "Group calls into the ledger"],
  alerts: [5, "Alert checks"],
  jev_analyst: [2, "Jev: direction odds per coin"], jev_scout: [2, "Jev: scores candidate trades, paper book"],
  jev_grade: [3, "Grades Jev's candidates"], factor_rank: [2, "Factor rank: coins by relative strength"],
  onchain_mvrv: [0, "Bitcoin MVRV Z-score"], events_macro: [1, "Calendar: FOMC and US data dates"],
  events_listings: [1, "Calendar: exchange listings and delistings"], events_polymarket: [1, "Calendar: Polymarket launch odds"],
  events_news: [1, "Calendar: dates found in the news"], events_oracle: [1, "Calendar: Oracle's 30-day research"],
  deep_discovery: [6, "Discovery: new and trending tokens"], deep_compose: [6, "Dossiers and the Albatross take"],
  deep_grade: [6, "Grades the dossiers against price"], deep_cryptopanic: [6, "CryptoPanic headlines"],
  deep_chain_jupiter: [6, "Jupiter: token security and holders"], deep_chain_rugcheck: [6, "RugCheck: insiders and LP"],
  deep_chain_birdeye: [6, "Birdeye: traders and EVM security"], deep_chain_honeypot: [6, "Honeypot and tax checks"],
  deep_market_stats: [6, "Market stats"], deep_market_dexpaid: [6, "Dex-paid checks"],
  deep_market_candles_hot: [6, "Candles for hot tokens"], deep_market_candles_warm: [6, "Candles for warm tokens"],
  deep_x_scan: [6, "X Scout: per-coin X reads"], deep_fit_research: [6, "Culture-fit research"],
  deep_github: [6, "GitHub: repo metadata, never the code"], deep_reddit_scan: [6, "Reddit scout, through Google"],
  deep_fomo_leaderboard: [6, "FOMO: top-trader boards, once a day"], deep_fomo_feed: [6, "FOMO: live trades and theses"],
  deep_fomo_holders: [6, "FOMO: who holds our hot coins"], deep_wallets: [6, "Wallets: labels and co-trading clusters"],
  deep_funders: [6, "Wallets: who funded each fresh wallet"],
};
function jobInfo(id) {
  const c = id.match(/^candles_(hl|okx|cb)_(\w+)$/);
  if (c) return [0, `${VENUE[c[1]]} ${CANDLE_TF[c[2]] || c[2]} candles`];
  return JOB[id] || [id.startsWith("micro_") ? 4 : id.startsWith("deep_") ? 6 : 2, humanize(id)];
}
const cadence = (s) => (s == null ? "manual or one-off" : s < 3600 ? `every ${s / 60} min` : s === 3600 ? "hourly"
  : s < 86400 ? `every ${s / 3600} h` : s === 86400 ? "daily" : s === 604800 ? "weekly" : `every ${Math.round(s / 86400)} days`);
const JOB_STATE = {
  failing: (j) => ["critical", `✕ Failing ×${j.consecutive_errors}`], late: () => ["warning", "◷ Late"],
  ok: () => ["good", "✓ On time"], manual: (j) => ["muted", j.consecutive_errors ? "✕ Last run failed" : "Manual"],
  paused: () => ["muted", "Paused"],
};
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;
const usd2 = (v) => (v > 0 && v < 0.01 ? "<$0.01" : `$${Number(v || 0).toFixed(2)}`);

async function loadHealth() {
  const h = state.health = await get("/api/health");
  const c = h.counts;
  const px = h.jobs.find((j) => j.job === "derivs_hl");
  $("#meta").textContent = `${c.assets} coins tracked` + (px?.last_ok ? ` · prices updated ${ago(px.last_ok)}` : "");
  const cards = [["Coins tracked", c.assets], ["Candles", c.candles], ["Derivatives snapshots", c.deriv_snapshots], ["Headlines", c.news], ["Indicator rows", c.features], ["Signals", c.signals]];
  $("#counts").innerHTML = cards.map(([k, v]) => `<div class="card"><div class="v">${v == null ? "–" : Number(v).toLocaleString()}</div><div class="k">${k}</div></div>`).join("");
  $("#counts-sum").textContent = `${Number(c.candles || 0).toLocaleString()} candles, ${Number(c.signals || 0).toLocaleString()} signals` +
    (c.collecting_since ? ` since ${utc(c.collecting_since).slice(0, 10)}` : "");

  const rank = { failing: 0, late: 1, ok: 2, manual: 3 };
  const bad = h.jobs.filter((j) => j.state === "failing" || j.state === "late");
  const groups = [["Needs a look", bad], ...JOB_AREAS.map((area, i) => [area, h.jobs.filter((j) => !bad.includes(j) && j.state !== "manual" && jobInfo(j.job)[0] === i)]),
    ["Manual and one-off", h.jobs.filter((j) => j.state === "manual")]];
  $("#jobs").innerHTML = `<thead><tr><th class="left">Job</th><th class="left">Status</th><th>Last success</th><th class="wide" title="Rows written by the last run">Rows</th></tr></thead><tbody>` +
    groups.filter(([, js]) => js.length).map(([area, js]) => `<tr class="grp"><td colspan="4">${esc(area)}</td></tr>` +
      js.sort((a, b) => rank[a.state] - rank[b.state] || a.job.localeCompare(b.job)).map((j) => {
        const [tone, label] = (JOB_STATE[j.state] || JOB_STATE.ok)(j);
        const err = j.consecutive_errors ? j.last_error || "" : "";
        return `<tr class="job ${esc(j.state)}"><td class="left name">${esc(jobInfo(j.job)[1])}<span class="sub mono">${esc(j.job)}</span>${
          err ? `<span class="sub jerr" title="${esc(err)}">Last error: ${esc(err.length > 160 ? `${err.slice(0, 160)}…` : err)}</span>` : ""}</td>
          <td class="left"><span class="jstate ${tone}">${label}</span></td>
          <td title="${j.last_ok ? `Last success ${utc(j.last_ok)}` : "Never succeeded"}">${ago(j.last_ok)}<span class="sub">${cadence(j.every_s)}</span></td>
          <td class="wide">${j.last_rows == null ? "–" : Number(j.last_rows).toLocaleString()}</td></tr>`;
      }).join("")).join("") + "</tbody>";
  const sched = h.jobs.filter((j) => j.state !== "manual").length;
  $("#jobs-sum").textContent = bad.length ? `${bad.length} of ${sched} need a look: ${bad.map((j) => jobInfo(j.job)[1].split(":")[0]).join(", ")}`
    : `all ${sched} scheduled jobs on time`;
  const fold = $("#jobs-fold");
  if (bad.length && !fold.dataset.bad) fold.open = true;     // opens once when trouble starts; closing it sticks
  fold.dataset.bad = bad.length ? "1" : "";

  const sp = h.agent_spend_today, calls = sp.reduce((a, s) => a + s.calls, 0), fails = sp.reduce((a, s) => a + s.failures, 0);
  const cost = sp.reduce((a, s) => a + Number(s.cost_usd || 0), 0);
  const failCell = (f, n) => `${f.toLocaleString()}${f && n ? `<span class="sub">${Math.round((f / n) * 100)}%</span>` : ""}`;
  $("#spend").innerHTML = `<thead><tr><th class="left">Agent</th><th>Calls</th><th>Failed</th><th>Cost</th></tr></thead><tbody>` +
    (sp.map((s) => `<tr><td class="left">${esc(srcLabel(s.agent))}</td><td>${s.calls.toLocaleString()}</td><td>${failCell(s.failures, s.calls)}</td><td>${usd2(s.cost_usd)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="left">No agent calls yet today (UTC).</td></tr>`) +
    (sp.length ? `<tr class="total"><td class="left">Total</td><td>${calls.toLocaleString()}</td><td>${failCell(fails, calls)}</td><td>${usd2(cost)}</td></tr>` : "") + "</tbody>";
  $("#spend-sum").textContent = calls ? `${usd2(cost)} across ${plural(calls, "call")}, ${fails.toLocaleString()} failed` : "no calls yet today";
  renderFomoVerdict(h.fomo_verdict);
  renderStatus();
}

// The FOMO subscription test (albatross/deepend/fomo_verdict.py): is a paid fomoapi.io plan worth it? Daily.
const VERDICT = { too_early: ["Too early", "muted"], inconclusive: ["Inconclusive", "warning"],
  buy_starter: ["Buy Starter", "good"], dont_buy: ["Don't buy", "critical"] };
function renderFomoVerdict(v) {
  const box = $("#fomo-verdict");
  if (!box) return;
  if (!v) { box.innerHTML = `<p class="note">No report yet: the test runs daily at 02:10 UTC.</p>`; $("#fomo-sum").textContent = "no report yet"; return; }
  const [word, tone] = VERDICT[v.decision?.verdict] || [v.decision?.verdict || "–", "muted"];
  const f = v.fomo || {}, tg = v.telegram_all || {}, rnd = v.vs_random || {}, cr = v.credits || {};
  const pct0 = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
  const ci = (i, unit = "") => (i ? `${i.value > 0 ? "+" : ""}${i.value}${unit} <span class="sub">(${i.lo} to ${i.hi})</span>` : "–");
  $("#fomo-sum").textContent = `${word.toLowerCase()} · ${(f.graded ?? 0).toLocaleString()} of ${v.min_tokens} FOMO tokens graded`;
  box.className = "fv";
  box.innerHTML = `<p><span class="jstate ${tone}">${esc(word)}</span> <span class="sub">as of ${esc(ago(v.computed_at))}</span></p>
    <ul class="vlist">${(v.decision?.why || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    <div class="fv-rows">${[["FOMO ranked traders' buys", "ranks 1-150", f], ["Telegram calls", "same period", tg]].map(([name, sub, x]) => `
      <div class="fv-row"><div class="fv-name">${name} <span class="sub">${sub}</span></div>
        <div class="fv-nums"><span><b>${(x.graded ?? 0).toLocaleString()}</b> of ${(x.tokens ?? 0).toLocaleString()} graded</span>
          <span><b>${pct0(x.hit_2x)}</b> reached 2x</span><span><b>${x.median_24h_pct == null ? "–" : `${x.median_24h_pct}%`}</b> median 24 h</span></div></div>`).join("")}</div>
    <p class="note">${rnd.tokens ? `Against a random coin bought the same minute (same chain, similar liquidity and age): 2x rate ${ci(rnd.hit_2x)}, median 24 h ${ci(rnd.net_24h_pct, " points")} over ${rnd.tokens.toLocaleString()} tokens.`
        : "No FOMO buy has a graded random coin to compare with yet: each needs its 24 hours."}
      The free credits served ${pct0(cr.lookup_coverage)} of the hot coins due a holder lookup, and ran short (under half served) on ${cr.days_short ?? 0} of the last 14 days.
      Starter is worth buying only if FOMO beats both Telegram and random entry with the interval clear of zero on 200+ tokens, adds to Telegram calls, and the free credits run out.</p>`;
}

// ─── alerts ────────────────────────────────────────────────────────────────
// Severity is always spelled out next to its icon and colour.
const SEV = { critical: ["⛔", "Critical"], warning: ["⚠", "Warning"] };
const sevBadge = (s) => `<span class="sev ${esc(s)}"><span aria-hidden="true">${SEV[s]?.[0] || "•"}</span> ${esc(SEV[s]?.[1] || s)}</span>`;
function lasted(a, b) {
  const m = Math.max(0, Math.round((new Date(b) - new Date(a)) / 60000));
  return m >= 1440 ? `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h` : m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

async function loadAlerts() {
  const a = state.alerts = await get("/api/alerts");
  const b = $("#alert-banner");
  const crit = a.open.filter((x) => x.severity === "critical").length, warn = a.open.length - crit;
  const worst = crit ? "critical" : warn ? "warning" : "";
  showBanner();
  const sys = $(".tabs .sys");                        // and the rail's System button carries a dot
  if (sys) {
    if (worst) sys.dataset.alert = worst; else delete sys.dataset.alert;
    sys.title = worst ? `Ops: ${plural(a.open.length, "open alert")}` : "Ops: agents, spend and the ChatGPT allowance, health and alerts";
    if (worst) sys.setAttribute("aria-label", `Ops, ${plural(a.open.length, "open alert")}`); else sys.removeAttribute("aria-label");
  }
  if (worst) {
    b.className = `alert-banner ${worst}`;
    b.innerHTML = `<span class="sev-icon" aria-hidden="true">${SEV[worst][0]}</span>
      <b>${[crit && `${crit} critical`, warn && `${warn} warning${warn === 1 ? "" : "s"}`].filter(Boolean).join(", ")}</b>
      <span class="alert-text">${esc(a.open.map((x) => x.title).join(" · "))}</span><span class="alert-go">Details →</span>`;
    b.title = a.open.map((x) => `${SEV[x.severity]?.[1] || x.severity}: ${x.title}`).join("\n");
  }
  renderAlerts();
}

// On every view but Health, which leads with the same news.
const showBanner = () => ($("#alert-banner").hidden = !state.alerts?.open.length || state.tab === "health");
document.addEventListener("viewchange", showBanner);

function renderAlerts() {
  const a = state.alerts;
  if (!a) return;
  $("#alerts-box").hidden = !a.open.length;
  // On a phone the narrow columns fold into the alert's own cell (.wide / .narrow).
  $("#alerts-open").innerHTML = `<thead><tr><th class="left">Severity</th><th class="left">Alert</th><th class="wide">For</th><th class="wide">Last seen</th></tr></thead><tbody>` +
    a.open.map((x) => {
      const since = x.since || x.first_seen;
      return `<tr><td class="left">${sevBadge(x.severity)}</td>
        <td class="left why"><b>${esc(x.title)}</b><span class="sub">${esc(x.detail || "")}</span>
          <span class="sub narrow">Open for ${lasted(since, Date.now())} · last seen ${ago(x.last_seen)}</span></td>
        <td class="wide">${lasted(since, Date.now())}<span class="sub">since ${utc(since)}</span></td><td class="wide">${ago(x.last_seen)}</td></tr>`;
    }).join("") + "</tbody>";
  $("#alerts-resolved").innerHTML = `<thead><tr><th class="left">Severity</th><th class="left">Alert</th><th class="wide">Began</th><th>Resolved</th><th>Lasted</th></tr></thead><tbody>` +
    (a.resolved.map((x) => `<tr><td class="left">${sevBadge(x.severity)}</td><td class="left why">${esc(x.title)}</td>
      <td class="wide">${utc(x.since)}</td><td>${ago(x.resolved_at)}<span class="sub">${utc(x.resolved_at)}</span></td><td>${lasted(x.since, x.resolved_at)}</td></tr>`).join("") ||
      `<tr><td colspan="5" class="left">Nothing resolved in the last 7 days.</td></tr>`) + "</tbody>";
  $("#resolved-sum").textContent = a.resolved.length ? plural(a.resolved.length, "incident") : "none";
  renderStatus();
}

// The one card at the top of Health. Needs the alerts; the job and spend figures fill in when they arrive.
function renderStatus() {
  const a = state.alerts, h = state.health;
  if (!a) return;
  const crit = a.open.filter((x) => x.severity === "critical").length, warn = a.open.length - crit;
  const jobs = h?.jobs.filter((j) => j.state !== "manual" && j.state !== "paused") || [];
  const onTime = jobs.filter((j) => j.state === "ok").length, retrying = jobs.filter((j) => j.state === "failing");
  let tone, kicker, title, sub;
  if (!a.checked_at) {
    [tone, kicker, title] = ["muted", "Status unknown", "The alert checks haven't run yet."];
    sub = "They start with the ingest scheduler and run every 5 minutes.";
  } else if (a.open.length) {
    [tone, kicker] = crit ? ["critical", "Needs attention now"] : ["warning", "Needs a look"];
    title = [crit && `${crit} critical problem${crit === 1 ? "" : "s"}`, warn && `${warn} warning${warn === 1 ? "" : "s"}`].filter(Boolean).join(" and ") + ".";
    sub = `${a.open[0].title}${a.open.length > 1 ? ` and ${a.open.length - 1} more` : ""}: details below. Checked ${ago(a.checked_at)}.`;
  } else if (retrying.length) {
    [tone, kicker, title] = ["warning", "Running", `Running, with ${plural(retrying.length, "job")} retrying.`];
    sub = `${retrying.map((j) => jobInfo(j.job)[1].split(":")[0]).join(", ")} failed ${retrying.length === 1 ? "its" : "their"} last run. ` +
      `A job becomes an alert after four failures in a row or 30 minutes. Checked ${ago(a.checked_at)}.`;
  } else {
    [tone, kicker, title] = ["good", "All systems normal", "Everything is running."];
    sub = `The alert checks ran ${ago(a.checked_at)} and found nothing wrong. They run every 5 minutes.`;
  }
  const sp = h?.agent_spend_today || [], calls = sp.reduce((x, s) => x + s.calls, 0), fails = sp.reduce((x, s) => x + s.failures, 0);
  if (!h && healthLoad.error) sub += ` The job list couldn't load: ${healthLoad.error}.`;
  const stat = (v, k, fold) => `<button class="stat" data-open="${fold}"><span class="v">${v}</span><span class="k">${k}</span></button>`;
  $("#sys-status").className = `sys-status ${tone}`;
  $("#sys-status").innerHTML = `<p class="kicker"><span class="dot" aria-hidden="true"></span>${esc(kicker)}</p>
    <h2>${esc(title)}</h2><p class="sub">${esc(sub)}</p>
    <div class="stats">
      ${stat(h ? `${onTime}<small> / ${jobs.length}</small>` : "–", "jobs on time", "jobs-fold")}
      ${stat(a.open.length, `open alert${a.open.length === 1 ? "" : "s"}`, a.open.length ? "alerts-box" : "resolved-fold")}
      ${stat(h ? `${fails}<small> / ${calls.toLocaleString()}</small>` : "–", "agent calls failed today", "spend-fold")}
      ${stat(h ? usd2(sp.reduce((x, s) => x + Number(s.cost_usd || 0), 0)) : "–", "agent spend today", "spend-fold")}
    </div>`;
}
$("#tab-health").addEventListener("click", (e) => {
  const b = e.target.closest("[data-open]");
  const el = b && document.getElementById(b.dataset.open);
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
});

// ─── social ────────────────────────────────────────────────────────────────
const VOLUME = ["Low", "Normal", "Elevated", "Extreme"];
const CROWDING = { none: "", moderate: "Some crowding", one_sided_bullish: "Crowded long", one_sided_bearish: "Crowded short" };
const mood5 = (v) => (v == null ? null : Math.round(v * 5));      // stored as -1..1; Grok reads on -5..+5
const moodLabel = (v) => (v == null ? "–" : `${v > 0 ? "▲" : v < 0 ? "▼" : ""}${Math.abs(mood5(v))}`);
const bigMood = (v) => (v == null ? "–" : `${v ? `<span class="arr">${v > 0 ? "▲" : "▼"}</span>` : ""}${Math.abs(mood5(v))}`);
// In the words of Social Pulse's own anchors: ±1 mildly, ±3 clearly, ±5 euphoric / capitulation.
function moodWord(v) {
  const m = mood5(v);
  if (m == null) return "";
  if (!m) return "mixed or neutral";
  if (Math.abs(m) >= 5) return m > 0 ? "euphoric" : "capitulation";
  return ["", "mildly ", "", "clearly ", "strongly "][Math.abs(m)] + (m > 0 ? "bullish" : "bearish");
}

// The crowd against the chart: X mood's side vs the quant bias on 1h and 1d. Neutral on either side is no verdict.
function versus(s) {
  const m = Math.sign(mood5(s.latest.sentiment) ?? 0);
  const sides = ["1h", "1d"].filter((t) => s.quant[t]?.bias);
  if (!m || !sides.length) return [`<span class="cat">–</span>`, "No verdict: X or the chart is neutral."];
  const off = sides.filter((t) => Math.sign(s.quant[t].bias) !== m), lean = m > 0 ? "bullish" : "bearish";
  return off.length ? [`<span class="vs split">≠ Split</span>`, `X leans ${lean}; the chart disagrees on ${off.join(" and ")}.`]
    : [`<span class="vs">= Agree</span>`, `X leans ${lean}, and so does the chart on ${sides.join(" and ")}.`];
}

async function loadSocial() {
  state.social = await get(`/api/social?days=${state.socialDays}`);
  renderSocial();
}

const HEAD_KIND = { flow: "Flow", catalyst: "Catalyst", positioning: "Positioning", launch: "Launch", narrative: "Narrative", risk: "Risk" };
const xLink = (url, label = "Post ↗") => (url && /^https:\/\/(x|twitter)\.com\//.test(url)
  ? `<a class="xs-post" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : "");
const coinLink = (sym) => {
  const t = String(sym || "").replace(/^\$/, "").toUpperCase();
  return /^[A-Z0-9]{2,12}$/.test(t) ? `<a class="xs-coin" href="#/coin/${encodeURIComponent(t)}">${esc(t)}</a>` : "";
};
const moodDab = (v) => (v == null ? `<span class="xs-mood none">·</span>`
  : `<span class="xs-mood" style="--pole:${v < 0 ? "var(--bear)" : "var(--bull)"};--mag:${Math.max(.18, Math.min(1, Math.abs(v))).toFixed(2)}">${moodLabel(v)}</span>`);
const OLD_READ_H = 12;

// What makes a read worth a look first: something new flagged by the read itself, a one-sided crowd, a
// strong or sharply changed mood, loud chatter, X disagreeing with the chart. Fresh reads before old ones.
function xRelevance(s) {
  const l = s.latest, m = Math.abs(mood5(l.sentiment) ?? 0), d = s.previous == null ? 0 : Math.abs(mood5(l.sentiment) - mood5(s.previous));
  const hours = (Date.now() - new Date(l.ts)) / 36e5;
  return (hours > OLD_READ_H ? -100 : 0) + 3 * !!l.notable + 1.5 * /^one_sided/.test(l.crowding || "") + m / 2 + d / 2
    + ((l.volume ?? 0) >= 2) + (versus(s)[0].includes("split") ? 0.5 : 0) - hours / 24;
}

function renderSocial() {
  const d = state.social;
  if (!d) return;
  tips.clear();
  const mk = d.market, mood = $("#mood"), al = d.allowance;
  const score = mk?.sentiment;
  mood.hidden = !d.market_mood;
  mood.innerHTML = d.market_mood ? `<div class="xs-moodhead"><div class="mood-k">Market mood on X <span>· read ${ago(d.market_mood_ts)}</span></div>
      ${score != null ? `<span class="xs-mood-word">${moodDab(score)} ${esc(cap(moodWord(score)))}</span>` : ""}</div>
    <p class="mood-t">${esc(d.market_mood)}</p>
    ${al ? `<p class="xs-allow">${al.reads_24h} X read${al.reads_24h === 1 ? "" : "s"} in the last 24 hours, using $${al.spent_24h.toFixed(2)} of Grok allowance (paced to about $${al.daily.toFixed(2)} a day). Next read at :10 past the hour.</p>` : ""}` : "";

  const heads = mk?.headlines || [];
  $("#xs-headlines").innerHTML = `<h3 class="xs-h3">What matters now <span class="cat">${mk ? `from the ${ago(mk.ts)} pulse` : ""}</span></h3>` + (heads.length
    ? `<ol class="xs-heads">${heads.map((h) => `<li><span class="xs-kind k-${esc(h.kind)}">${esc(HEAD_KIND[h.kind] || h.kind)}</span>
        <p>${esc(h.text)}</p><div class="xs-hfoot">${(h.coins || []).map(coinLink).join("")}${xLink(h.url)}</div></li>`).join("")}</ol>`
    : `<p class="cat xs-empty">The hourly market pulse lists the few things on X a trader should know, each tied to its post. The first one lands at :10 past the hour.</p>`);

  const trend = mk?.trending || [];
  $("#xs-trending").innerHTML = `<h3 class="xs-h3">Trending on X <span class="cat">coins the crowd is suddenly on</span></h3>` + (trend.length
    ? `<ul class="xs-trend">${trend.map((t) => `<li>${coinLink(t.ticker) || `<b>${esc(t.ticker)}</b>`}<span>${esc(t.why)} ${xLink(t.url, "Post ↗")}</span></li>`).join("")}</ul>`
    : `<p class="cat xs-empty">Nothing yet: filled from the hourly pulse. A coin trending here gets its own read the next hour.</p>`);

  const hasSectors = d.scopes.some((s) => s.kind === "sector");
  $("#social-kind").hidden = !hasSectors;
  if (!hasSectors) state.socialKind = "asset";
  const kind = state.socialKind;
  const rows = d.scopes.filter((s) => s.kind === kind && s.latest).sort((a, b) => xRelevance(b) - xRelevance(a));
  const card = (s) => {
    const l = s.latest, name = s.kind === "asset" ? s.scope : s.name, old = (Date.now() - new Date(l.ts)) / 36e5 > OLD_READ_H;
    const chg = s.previous == null ? null : mood5(l.sentiment) - mood5(s.previous);
    const said = l.summary || (l.narratives || []).join(" · ") || "No summary in this read.";
    const crowd = CROWDING[l.crowding] || "", hot = crowd.startsWith("Crowded"), [vs] = versus(s);
    const vsText = vs.includes("split") ? "Splits from the chart" : vs.includes("Agree") ? "Agrees with the chart" : "";
    const tags = [chg ? `<span class="xs-chg ${chg > 0 ? "up" : "down"}">Mood ${chg > 0 ? "▲" : "▼"}${Math.abs(chg)} since the last read</span>` : "",
      `${VOLUME[Math.round(l.volume ?? -1)] || "Unknown"} chatter`,
      crowd ? `<span class="${hot ? "hot" : ""}">${hot ? "⚠ " : ""}${esc(crowd)}</span>` : "",
      vsText ? `<span class="${vsText.startsWith("Splits") ? "hot" : ""}">${vsText}</span>` : "",
      l.posts_reviewed != null ? `${l.posts_reviewed} posts` : ""].filter(Boolean);
    return `<article class="xs-row${l.notable ? " notable" : ""}${old ? " old" : ""}" data-scope="${esc(s.scope)}" tabindex="0" role="button" aria-label="${esc(name)}: open the X read">
      <div class="xs-id"><span class="sym">${esc(name)}</span>${moodDab(l.sentiment)}</div>
      <div class="xs-say"><p class="xs-sum">${l.notable ? `<span class="xs-flag">New</span>` : ""}${esc(said)}</p>
        ${l.why_it_matters ? `<p class="xs-why">${esc(l.why_it_matters)}</p>` : ""}
        <div class="xs-tags">${tags.join("<i>·</i>")}</div></div>
      <div class="xs-when">${ago(l.ts)}</div></article>`;
  };
  const fresh = rows.filter((s) => (Date.now() - new Date(s.latest.ts)) / 36e5 <= OLD_READ_H), older = rows.filter((s) => !fresh.includes(s));
  const win = state.socialDays === 1 ? "24 hours" : `${state.socialDays} days`;
  const was = $("#social .xs-older"), keepOpen = was ? was.open : !fresh.length;    // the minute refresh must not fold it shut
  $("#social").innerHTML = rows.length
    ? fresh.map(card).join("") + (older.length ? `<details class="xs-older"${keepOpen ? " open" : ""}><summary>${older.length} older read${older.length === 1 ? "" : "s"} (over ${OLD_READ_H} hours old)</summary>${older.map(card).join("")}</details>` : "")
    : `<p class="xs-none">No X reads for ${kind === "asset" ? "coins" : "sectors"} in the last ${win}. Social Pulse reads the coins that matter each hour: Tier-1 coins every few hours, plus any coin that is moving, trending, in an open trade or in fresh news.</p>`;
}

function openSocial(scope) {
  const s = state.social?.scopes.find((x) => x.scope === scope);
  if (!s) return;
  const l = s.latest, name = s.kind === "asset" ? s.scope : s.name;
  const hist = s.history.filter((h) => h.sentiment != null);
  const chg = hist.length > 1 ? mood5(l.sentiment) - mood5(hist[0].sentiment) : null;
  const since = chg == null ? "the only read in this window"
    : `<span class="delta ${chg > 0 ? "up" : chg < 0 ? "down" : "flat"}">${chg > 0 ? "▲ " : chg < 0 ? "▼ " : ""}${Math.abs(chg)}</span> since ${utc(hist[0].ts)}`;
  const list = (xs) => `<ul class="plain-list">${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  const posts = (l.samples || []).map((p) => {
    const safe = p.url && /^https:\/\/(x|twitter)\.com\//.test(p.url);
    return `<li><div class="xp-a">${esc(p.author || "Unnamed account")}</div><p>${esc(p.text || "")}</p>
      ${safe ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">View on X ↗</a>` : ""}</li>`;
  }).join("");
  const crowd = CROWDING[l.crowding], tfs = ["1h", "4h", "1d"];
  $("#drawer-body").innerHTML = `
    <p class="kicker">X sentiment · ${s.kind === "asset" ? "Coin" : "Sector"}</p>
    <h2>${esc(name)}</h2>
    <div class="sx-head">
      <div class="sx-big"><span class="${l.sentiment > 0 ? "up" : l.sentiment < 0 ? "down" : ""}">${bigMood(l.sentiment)}</span><small>of ±5</small></div>
      <div><div class="sx-word">${esc(cap(moodWord(l.sentiment) || "no read"))}</div><div class="sx-since">${since}</div></div>
      <table class="sx-chart" aria-label="Quant chart bias"><thead><tr><th colspan="3">The chart (quant)</th></tr><tr>${tfs.map((t) => `<th>${t}</th>`).join("")}</tr></thead>
        <tbody><tr>${tfs.map((t) => cellHtml(s.quant[t], null, null)).join("")}</tr></tbody></table>
    </div>
    ${l.summary ? `<p class="sx-sum">${esc(l.summary)}</p>` : ""}${l.why_it_matters ? `<p class="xs-why">${esc(l.why_it_matters)}</p>` : ""}
    <p class="sx-facts">${l.posts_reviewed ?? "?"} posts reviewed · chatter ${esc((VOLUME[Math.round(l.volume ?? -1)] || "unknown").toLowerCase())}${crowd ? ` · ${esc(crowd.toLowerCase())}` : ""} · read ${utc(l.ts)}, ${ago(l.ts)}${(l.picked_for || []).length ? ` · read because: ${esc(l.picked_for.map((w) => ({ moving: "moving hard", trending: "trending on X", trade: "in an open trade", news: "in fresh news", due: "due for a read" }[w] || w)).join(", "))}` : ""}</p>
    <button class="btn-paint" id="sx-go">${s.kind === "asset" ? `Open the ${esc(name)} coin page →` : `See the ${esc(name)} coins →`}</button>
    <h3>Mood over time <span class="cat">${hist.length} read${hist.length === 1 ? "" : "s"}, −5 … +5</span></h3><div id="sx-chart"></div>
    <h3>Narratives</h3>${l.narratives?.length ? list(l.narratives) : `<p class="cat">None reported.</p>`}
    ${l.positioning?.length ? `<h3>Positioning <span class="cat">claims seen in posts</span></h3>${list(l.positioning)}` : ""}
    ${l.catalysts?.length ? `<h3>Catalysts <span class="cat">dates as posted</span></h3>${list(l.catalysts)}` : ""}
    <h3>Sample posts <span class="cat">from the latest read</span></h3><ul class="xposts">${posts || `<li class="cat">No posts stored.</li>`}</ul>`;
  drawChart($("#drawer-body #sx-chart"), hist.map((h) => ({ t: new Date(h.ts), v: h.sentiment * 5 })), [],
    { lo: -5, hi: 5, fmt: (v) => (v > 0 ? "+" : "") + (Number.isInteger(v) ? v : v.toFixed(1)), label: "Read" });
  $("#sx-go").addEventListener("click", () => {
    if (s.kind === "asset") return openAsset(s.scope);
    $("#drawer-close").click();
    openSector(s.scope.replace(/^sector:/, ""));
  });
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}

// ─── scorecard ─────────────────────────────────────────────────────────────
const pct = (v, d = 1) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
const scLabel = (s) => (s === "quant" ? "Live quant" : srcLabel(s));
const windowLabel = (days) => (days > 0 ? `last ${days} days` : "all history");

// Rough 95% band. Consecutive calls on one asset share most of their 6-bar window, so the
// effective sample is ~n / horizon; assets also move together, so the true band is wider still.
// The variance uses (hits + 1) / (n + 2), as grading/scorecard.vs_naive does, so 0 of 4 is not a
// zero-width band that calls itself significant.
function band(p, n, h) {
  if (!n) return null;
  const q = (p * n + 1) / (n + 2);
  return 1.96 * Math.sqrt((q * (1 - q)) / Math.max(1, n / h));
}

// A hit-rate heat cell. Below minN graded calls the rate is noise, so the cell shows the count, not a colour.
function hitCell(p, n, tipHtml, minN = 0) {
  if (p == null || !n) return `<td class="cell empty"><span>·</span></td>`;
  if (n < minN) {
    return `<td class="cell few" data-tipid="${tipId(`${tipHtml}<div class="tip-text">Only ${n} graded: too few to colour (${minN} needed).</div>`)}"><span>${n} call${n === 1 ? "" : "s"}</span></td>`;
  }
  const pole = p < 0.5 ? "var(--bear)" : "var(--bull)";
  const mag = Math.min(1, Math.abs(p - 0.5) / 0.15).toFixed(2);
  return `<td class="cell" data-tipid="${tipId(tipHtml)}"><span style="--pole:${pole};--mag:${mag}">${(p * 100).toFixed(0)}%</span></td>`;
}

function hitBar(p, ci) {
  if (p == null) return "–";
  const X = (v) => Math.max(0, Math.min(100, (v - 0.3) / 0.4 * 100));  // axis 30% … 70%
  const a = X(Math.min(p, 0.5)), b = X(Math.max(p, 0.5));
  const lo = ci == null ? null : X(p - ci), hi = ci == null ? null : X(p + ci);
  return `<span class="hitbar" aria-hidden="true"><i class="mid"></i><b style="left:${a}%;width:${b - a}%;background:${p < 0.5 ? "var(--bear)" : "var(--bull)"}"></b>
    ${lo == null ? "" : `<s style="left:${lo}%;width:${hi - lo}%"></s>`}</span>`;
}

// The signal side of the Track record: the "which sources beat naive" answer leads, the one-source
// detail (cards, by timeframe, by strength, by sector) sits in a fold under it.
async function loadScorecard() {
  const [d, lb] = await Promise.all([
    get(`/api/scorecard?source=${encodeURIComponent(state.scSource)}&days=${state.scDays}`),
    get(`/api/scorecard/sources?days=${state.scDays}`),
  ]);
  state.sc = d;
  state.scLb = lb;
  tips.clear();
  renderSourcesAnswer();
  $("#sc-source").innerHTML = d.sources.map((src) =>
    `<button data-src="${esc(src)}" class="${src === state.scSource ? "on" : ""}">${esc(scLabel(src))}</button>`).join("");
  $("#sc-fold-cat").textContent = `${scLabel(state.scSource)} · ${windowLabel(state.scDays)}`;
  const h = d.horizon_bars || 6, min = d.min_n || 20;
  const tf = d.by_tf;
  const n = tf.reduce((a, r) => a + r.n, 0);
  const wavg = (k) => (n ? tf.reduce((a, r) => a + (r[k] ?? 0) * r.n, 0) / n : null);
  const nS = tf.reduce((a, r) => a + (r.n_strong || 0), 0);
  const hitS = nS ? tf.reduce((a, r) => a + (r.hit_rate_strong ?? 0) * (r.n_strong || 0), 0) / nS : null;
  $("#sc-cards").innerHTML = [
    ["Graded calls", n.toLocaleString()], ["Hit rate", pct(wavg("hit_rate"))], ["Always-long would score", pct(wavg("long_rate"))],
    ["Strong-call hit rate (±2)", pct(hitS)], ["Avg return per call", fmtPct(wavg("avg_return_pct"), 2)],
  ].map(([k, v]) => `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

  $("#sc-tf").innerHTML = `<thead><tr><th>TF</th><th>Calls</th><th class="left">Hit rate <span class="cat">(axis 30–70%, line = 50%)</span></th><th title="Share of the same windows where price rose">Always-long</th>
    <th title="Hit rate minus the better of always-long / always-short">Edge vs one-way</th><th class="left">Verdict</th><th>Strong ±2</th><th>Avg return</th><th>Avg MFE</th><th>Avg MAE</th><th>Invalidated</th><th class="left">Calls from</th></tr></thead><tbody>` +
    (tf.map((r) => {
      const ci = band(r.hit_rate, r.n, h);
      const naive = Math.max(r.long_rate ?? 0.5, 1 - (r.long_rate ?? 0.5));
      const edge = r.hit_rate - naive;
      const verdict = r.n < min ? "· Too few to judge" : Math.abs(edge) <= ci ? "· Noise" : edge > 0 ? "▲ Edge" : "▼ Worse than one-way";
      return `<tr><td class="sym">${esc(r.tf)}</td><td>${r.n.toLocaleString()}</td><td class="left"><span class="hitcell">${hitBar(r.hit_rate, ci)}<span>${pct(r.hit_rate)} <span class="cat">±${(ci * 100).toFixed(1)}</span></span></span></td><td>${pct(r.long_rate)}</td>
        <td>${edge > 0 ? "+" : ""}${(edge * 100).toFixed(1)} points</td><td class="left">${verdict}</td>
        <td>${pct(r.hit_rate_strong)} <span class="cat">${(r.n_strong || 0).toLocaleString()}</span></td>
        <td>${fmtPct(r.avg_return_pct, 2)}</td><td>${fmtPct(r.avg_mfe_pct, 2)}</td><td>${fmtPct(r.avg_mae_pct, 2)}</td><td>${pct(r.invalidated_rate, 0)}</td>
        <td class="left cat" title="${utc(r.first_call)} → ${utc(r.last_call)}">${utc(r.first_call).slice(5, 10)} → ${utc(r.last_call).slice(5, 10)}</td></tr>`;
    }).join("") || `<tr><td colspan="12" style="text-align:left">No graded calls for this source and window.</td></tr>`) + "</tbody>";
  $("#sc-note").textContent = `A call "hits" when price moved its way over the next ${h} bars of its own timeframe. ` +
    `Beating 50% is not enough in a trending market: the benchmark is the better of always-long and always-short over the same windows. ` +
    `The ± is a rough 95% band that treats overlapping windows as ~1/${h} of a sample; assets move together, so real uncertainty is larger. "Noise" means the edge sits inside that band; nothing is judged below ${min} graded calls.`;

  const biases = [-2, -1, 1, 2];
  const bb = new Map(d.by_bias.map((r) => [`${r.tf}|${r.bias}`, r]));
  $("#sc-bias").innerHTML = `<thead><tr><th>TF</th>${biases.map((b) => `<th>${b > 0 ? "▲ Long " : "▼ Short "}${Math.abs(b) === 2 ? "strong" : "weak"}</th>`).join("")}</tr></thead><tbody>` +
    (TFS.filter((t) => tf.some((r) => r.tf === t)).map((t) => `<tr><td class="sym">${t}</td>${biases.map((b) => {
      const r = bb.get(`${t}|${b}`);
      return r ? hitCell(r.hit_rate, r.n, `<b>${t} · ${b > 0 ? "long" : "short"} ${Math.abs(b)}</b>` + row("Hit rate", pct(r.hit_rate)) + row("Calls", r.n.toLocaleString()) + row("Avg return", fmtPct(r.avg_return_pct, 2)), min) : hitCell(null);
    }).join("")}</tr>`).join("") || `<tr><td colspan="5" class="left">No graded calls for this source and window.</td></tr>`) + "</tbody>";

  sectorMatrix("#sc-sector", d.by_sector, "Not enough graded calls per sector yet.", min);
  $("#sc-sector-note").textContent = `A sector shows on a timeframe once it has ${min} graded asset calls there. Hover a cell for the count, the always-long benchmark and the average return.`;
  sectorMatrix("#sc-sector-calls", d.sector_calls || [],
    state.scSource === "quant" ? "No graded sector calls yet." : "Only the live quant model makes sector calls.", min);
}

// Sector × timeframe hit-rate matrix, one row per sector, busiest first.
function sectorMatrix(sel, rows, empty, minN) {
  const secs = new Map();
  for (const r of rows) {
    const s = secs.get(r.sector) || { name: r.name, n: 0, tfs: {} };
    s.n += r.n; s.tfs[r.tf] = r; secs.set(r.sector, s);
  }
  const tfCols = TFS.filter((t) => rows.some((r) => r.tf === t));
  $(sel).innerHTML = `<thead><tr><th>Sector</th><th>Calls</th>${tfCols.map((t) => `<th>${t}</th>`).join("")}</tr></thead><tbody>` +
    ([...secs.values()].sort((a, b) => b.n - a.n).map((s) => `<tr><td class="sym">${esc(s.name)}</td><td>${s.n.toLocaleString()}</td>${tfCols.map((t) => {
      const r = s.tfs[t];
      return r ? hitCell(r.hit_rate, r.n, `<b>${esc(s.name)} · ${t}</b>` + row("Hit rate", pct(r.hit_rate)) + row("Always-long", pct(r.long_rate)) + row("Calls", r.n.toLocaleString()) + row("Avg return", fmtPct(r.avg_return_pct, 2)), minN) : hitCell(null);
    }).join("")}</tr>`).join("") || `<tr><td colspan="8" style="text-align:left">${esc(empty)}</td></tr>`) + "</tbody>";
}

// "Which sources beat a naive caller?": every live source × timeframe, judged as the reports judge it.
const LB_ORDER = ["strategist", "ensemble", "quant"];
const srcRank = (src) => (LB_ORDER.includes(src) ? LB_ORDER.indexOf(src) : 9);
function renderSourcesAnswer() {
  const d = state.scLb;
  if (!d) return;
  const min = d.min_n, h = d.horizon_bars || 6, rows = d.rows;
  const srcs = [...new Set(rows.map((r) => r.source))].sort((a, b) => srcRank(a) - srcRank(b) || scLabel(a).localeCompare(scLabel(b)));
  const tfs = TFS.filter((t) => rows.some((r) => r.tf === t));
  const at = new Map(rows.map((r) => [`${r.source}|${r.tf}`, r]));
  const judged = rows.filter((r) => r.n >= min);
  const better = judged.filter((r) => r.verdict === "better"), worse = judged.filter((r) => r.verdict === "worse");
  const few = rows.length - judged.length;
  const names = (rs) => srcs.filter((s) => rs.some((r) => r.source === s))
    .map((s) => `${scLabel(s)} ${rs.filter((r) => r.source === s).map((r) => r.tf).join(" and ")}`).join(", ");
  const rest = `everything else is within the noise band${few ? " or has too few graded calls" : ""}`;
  const verdict = !rows.length ? `<b>Nothing graded yet</b> in the ${windowLabel(state.scDays)}.`
    : better.length ? `<b>Yes: ${esc(names(better))}.</b> ${worse.length ? `Worse than a one-way caller: ${esc(names(worse))}; ` : ""}${cap(rest)}.`
    : `<b>None yet.</b> ${worse.length ? `${esc(names(worse))} ${worse.length === 1 ? "is" : "are"} worse than a one-way caller; ${rest}.` : `${cap(rest)}.`}`;
  const cell = (src, t) => {
    const r = at.get(`${src}|${t}`);
    if (!r) return `<td class="cell empty"><span>·</span></td>`;
    const naiveLong = r.long_rate >= 0.5, ci = band(r.hit_rate, r.n, h);
    const tip = `<b>${esc(scLabel(src))} · ${t}</b>` + row("Hit rate", pct(r.hit_rate)) +
      row(`One-way (always-${naiveLong ? "long" : "short"})`, pct(naiveLong ? r.long_rate : 1 - r.long_rate)) +
      row("Edge", `${sgn(r.edge * 100, 1)} points ± ${(ci * 100).toFixed(1)}`) + row("Graded calls", r.n.toLocaleString());
    if (r.n < min) return `<td class="cell few" data-tipid="${tipId(tip + `<div class="tip-text">Too few to judge: ${min} graded calls needed.</div>`)}"><span>${r.n} call${r.n === 1 ? "" : "s"}</span></td>`;
    const [mark, pole, mag] = r.verdict === "better" ? ["▲ ", "var(--bull)", Math.min(1, r.edge / 0.15)]
      : r.verdict === "worse" ? ["▼ ", "var(--bear)", Math.min(1, -r.edge / 0.15)] : ["", "var(--mid)", 0];
    const word = { better: "Better than a one-way caller, outside the band", worse: "Worse than a one-way caller, outside the band", noise: "Inside the noise band" }[r.verdict];
    return `<td class="cell" data-tipid="${tipId(tip + `<div class="tip-text">${word}.</div>`)}"><span style="--pole:${pole};--mag:${mag.toFixed(2)}">${mark}${sgn(r.edge * 100, 1)}</span></td>`;
  };
  $("#ans-sources-body").innerHTML = `<p class="answer-verdict">${verdict}</p>` + (rows.length ? `
    <div class="table-wrap bare"><table class="matrix lb"><thead><tr><th>Source</th>${tfs.map((t) => `<th>${t}</th>`).join("")}</tr></thead>
    <tbody>${srcs.map((s) => `<tr data-lbsrc="${esc(s)}" tabindex="0" title="Open ${esc(scLabel(s))} in detail"><td class="sym">${esc(scLabel(s))}</td>${tfs.map((t) => cell(s, t)).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="answer-foot">Each cell is the hit rate minus the better of always-long and always-short on the same windows, in points. ▲ ahead and ▼ behind lie outside a rough 95% band; a plain number is inside it, so no different from a one-way caller. Fewer than ${min} graded calls shows the count instead. Live sources only, ${windowLabel(state.scDays)}. Click a source for its detail.</p>` : "");
}

// ─── ideas (the call ledger) ───────────────────────────────────────────────
const EXIT_LABEL = { target: "✓ Target", stop: "✕ Stop", horizon: "◷ Horizon" };
const UNGRADEABLE = {
  no_dex_cost_model: "no cost model for DEX trades yet", no_spot_cost_model: "no cost model for spot trades yet",
  no_candles: "price data still missing 2 days past the horizon", bad_levels: "stop or target on the wrong side of the entry price",
};
// What the critic did with an idea: [table label, word for sentences, tooltip].
const CRITIC = {
  keep: ["✓ Kept", "kept", "No serious objection: logged as proposed"],
  downgrade: ["↓ Downgraded", "downgraded", "Let through at 60% of the Strategist's confidence"],
  veto: ["✕ Vetoed", "vetoed", "Blocked, but still logged and graded so the critic itself can be judged"],
};
// Only the Strategist's ideas go through the critic (agents/strategist.py _critique); the Oracle's never do.
const CRITIC_SOURCES = new Set(["strategist"]);
const criticOf = (c) => (c.critique ? CRITIC[c.critique.verdict] || [humanize(c.critique.verdict), c.critique.verdict, ""]
  : c.status === "vetoed" ? CRITIC.veto
  : CRITIC_SOURCES.has(c.source_id) ? ["No verdict", "unreviewed", "The critic was unavailable when this was logged"]
  : ["Not reviewed", "unreviewed", `${srcLabel(c.source_id)} ideas don't go through the critic`]);
// What woke the Oracle up (oracle.py triggers), from the key it stores in the idea's evidence.
const TRIGGER = { news: "a high-impact headline", move: "an outsized price move", flip: "a bias flip", conflict: "its sources disagreeing" };
const IDEAS_LIMIT = 500;
const isOpenIdea = (c) => c.outcome !== "graded" && c.outcome !== "ungradeable";   // null: not marked yet
// Signed with a true minus, and toned only when it survives rounding (+0.00 is neither up nor down).
const sgn = (v, d = 2) => { const x = Number(v).toFixed(d); return +x > 0 ? `+${x}` : +x < 0 ? `−${x.slice(1)}` : Math.abs(+x).toFixed(d); };
const tone = (v, d = 2) => (v == null || +Number(v).toFixed(d) === 0 ? "" : v > 0 ? "up" : "down");
const rCell = (v, cls = "") => (v == null ? "–" : `<span class="r ${tone(v)} ${cls}">${sgn(v)}R</span>`);
function timeLeft(ts) {
  const m = (new Date(ts) - Date.now()) / 60000;
  return m <= 0 ? "horizon passed" : m < 90 ? `${Math.round(m)}m left` : m < 2880 ? `${Math.round(m / 60)}h left` : `${Math.round(m / 1440)}d left`;
}

async function loadIdeas() {
  const q = new URLSearchParams({ limit: IDEAS_LIMIT });
  if (state.ideaStatus) q.set("status", state.ideaStatus);
  if (state.ideaSource) q.set("source", state.ideaSource);
  // The check rehashes the whole ledger (~600 micro-cap calls a day; cached 5 min): never hold the table for it.
  const verify = get("/api/ledger/verify").catch(() => null);
  let d;
  try { d = await get(`/api/ideas?${q}`); }
  catch (e) { if (!state.ideas) $("#ledger-line").hidden = true; throw e; }   // nothing loaded: the failure panel says it, not "Checking…"
  $("#ledger-line").hidden = false;
  state.ideas = d;
  const sel = $("#idea-source");
  sel.innerHTML = `<option value="">All sources</option>` + d.sources.map((s) =>
    `<option value="${esc(s.source_id)}" ${s.source_id === state.ideaSource ? "selected" : ""}>${esc(srcLabel(s.source_id))} (${s.n})</option>`).join("");
  sel.hidden = d.sources.length < 2 && !state.ideaSource;     // one source: nothing to choose
  renderIdeas();
  const v = await verify, majors = d.sources.reduce((a, s) => a + s.n, 0);
  if (!v) { $("#ledger-line").innerHTML = `<b>Ledger check unavailable</b> <span class="cat">· the hash chain could not be verified just now; it retries every minute.</span>`; return; }
  $("#ledger-line").innerHTML = v.broken_id == null
    ? `<b>✓ Ledger intact.</b> All ${v.calls.toLocaleString()} logged calls re-hashed ${ago(v.checked_at)}: none has been edited since it was written. ` +
      `<span class="cat">${majors.toLocaleString()} are trade ideas on major coins, listed here; the rest are micro-cap Telegram group calls, graded separately.</span>`
    : `<b>⚠ Ledger hash chain broken at call #${v.broken_id}</b> (${v.calls.toLocaleString()} calls verified before it): a logged call was altered after the fact. <span class="cat">checked ${ago(v.checked_at)}</span>`;
  $("#ledger-line").classList.toggle("alarm", v.broken_id != null);
}

const ideaTile = (k, v, dHtml, toneVar, title = "") =>
  `<div class="kpi" style="--tone:${toneVar}"${title ? ` title="${esc(title)}"` : ""}><div class="k">${k}</div><div class="v">${v}</div><div class="d">${dHtml}</div></div>`;
const rChip = (v) => (v == null ? "" : `<span class="delta ${tone(v) || "flat"}">${{ up: "▲ ", down: "▼ " }[tone(v)] || ""}${sgn(v)}R</span>`);
const toneVar = (v) => ({ up: "var(--bull)", down: "var(--bear)" }[tone(v)] || "var(--cream)");
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

function renderIdeas() {
  const d = state.ideas;
  if (!d) return;
  const open = d.ideas.filter(isOpenIdea), closed = d.ideas.filter((c) => !isOpenIdea(c));
  const graded = closed.filter((c) => c.outcome === "graded"), ungr = closed.length - graded.length;
  const openR = mean(open.map((c) => c.r_multiple).filter((v) => v != null));
  const closedR = mean(graded.map((c) => c.r_multiple).filter((v) => v != null));
  const won = graded.filter((c) => c.net_return_pct > 0).length;
  const verdicts = { kept: 0, downgraded: 0, vetoed: 0, unreviewed: 0 };
  d.ideas.forEach((c) => { verdicts[criticOf(c)[1]] = (verdicts[criticOf(c)[1]] || 0) + 1; });
  const R = "R: the net return as a multiple of the distance to the stop, so −1R is a full stop-out.";
  $("#ideas-kpis").innerHTML = d.ideas.length ? [
    ideaTile("Open now", open.length, !open.length ? "<span>no idea is running</span>" : openR == null ? "<span>not marked yet: the backtester runs every 5 minutes</span>"
      : `${rChip(openR)} <span>average if closed now, unrealised</span>`, toneVar(openR), R),
    ideaTile("Closed", graded.length, graded.length ? `${rChip(closedR)} <span>average · ${won} of ${graded.length} won${ungr ? ` · ${ungr} ungradeable` : ""}</span>`
      : `<span>none yet: results land as stops, targets and horizons are hit</span>`, toneVar(closedR), R),
    verdicts.unreviewed === d.ideas.length && !d.ideas.some((c) => CRITIC_SOURCES.has(c.source_id))
      ? ideaTile("Critic", "Not used", `<span>${esc([...new Set(d.ideas.map((c) => srcLabel(c.source_id)))].join(", "))} ideas don't go through the critic</span>`, "var(--cream)")
      : ideaTile("Critic", `${verdicts.vetoed} vetoed`, `<span>${verdicts.downgraded} downgraded · ${verdicts.kept} kept${verdicts.unreviewed ? ` · ${verdicts.unreviewed} not reviewed` : ""}</span>`, "var(--cream)"),
  ].join("") : "";

  const coins = new Map();
  open.forEach((c) => coins.set(c.scope, (coins.get(c.scope) || new Set()).add(c.tf)));
  const multi = [...coins].filter(([, t]) => t.size > 1);
  $("#ideas-open-cat").textContent = open.length ? `${open.length} idea${open.length === 1 ? "" : "s"} on ${coins.size} coin${coins.size === 1 ? "" : "s"}` : "";
  const why = multi.length ? `<p class="ideas-why">${multi.length} coin${multi.length === 1 ? "" : "s"} ${multi.length === 1 ? "has" : "have"} ideas on more than one timeframe (${esc(multi[0][0])} on ${[...multi[0][1]].sort((a, b) => TFS.indexOf(a) - TFS.indexOf(b)).join(" and ")}). ` +
    "They are separate trades, each with its own stop, target and horizon, not duplicates: the Strategist never logs a second idea for the same coin, side and timeframe while one is live.</p>" : "";
  const filtered = state.ideaStatus || state.ideaSource;
  $("#ideas").innerHTML = ideasTable(open,
    d.ideas.length ? "No idea is open right now; the closed ones are below." : `No ideas logged${filtered ? " for this filter" : " yet"}. The Strategist logs one when its 1h/4h/1d call is strong and reward:risk clears 1.5.`);
  document.querySelector("#tab-ideas .ideas-why")?.remove();
  if (why) $("#tab-ideas .ideas-h").insertAdjacentHTML("afterend", why);
  $("#ideas-closed").innerHTML = ideasTable(closed, "Nothing has closed yet.");
  if (!open.length && closed.length) $("#ideas-closed-fold").open = true;    // "the closed ones are below" must be true
  const exits = (r) => graded.filter((c) => c.exit_reason === r).length;
  $("#ideas-closed-sum").innerHTML = `Closed ideas <span class="cat">${closed.length ? `${graded.length} graded: ${exits("target")} hit a target, ${exits("stop")} stopped out, ${exits("horizon")} ran to the horizon${ungr ? ` · ${ungr} ungradeable` : ""}` : "none yet"}${d.ideas.length >= IDEAS_LIMIT ? ` · newest ${IDEAS_LIMIT} ideas only` : ""}</span>`;
  $("#ideas-note").textContent = "Every idea is written append-only and hash-chained before its outcome exists; vetoed ideas stay in the ledger so the backtest can show whether the critic helps. " +
    (d.graded ? "Results come from the backtester's stop/target policy (stop, first target, else exit at the horizon), net of fees, slippage and funding. Open ideas show what they would return if closed at the last candle, after exit costs: unrealised, and not a result. " : "Result columns fill in once the backtester is grading calls. ") +
    "R is the net return as a multiple of the distance to the stop. Click a row for the plan, the critic's reason and the thesis; click a coin for its chart.";
}

const IDEA_COLS = [["Coin", "left"], ["TF", "left"], ["Side", "left side-col"], ["Logged", "left wide"], ["Entry", "wide"], ["R:R", "wide"], ["Critic", "left"], ["Result", "left"], ["Net R", ""]];

// One group per coin, in the order the list arrives (newest first); inside a coin, by timeframe.
function ideasTable(list, empty) {
  const head = `<thead><tr>${IDEA_COLS.map(([c, cls]) => `<th class="${cls}">${c}</th>`).join("")}</tr></thead>`;
  if (!list.length) return head + `<tbody><tr><td colspan="${IDEA_COLS.length}" class="left empty-row">${esc(empty)}</td></tr></tbody>`;
  const groups = new Map();
  for (const c of list) groups.set(c.scope, [...(groups.get(c.scope) || []), c]);
  const body = [...groups].map(([scope, cs]) => {
    cs.sort((a, b) => TFS.indexOf(a.tf) - TFS.indexOf(b.tf) || new Date(b.logged_at) - new Date(a.logged_at));
    const span = cs.length + cs.filter((c) => state.openIdeas.has(c.id)).length;
    const nTf = new Set(cs.map((c) => c.tf)).size;
    const sub = cs.length < 2 ? "" : nTf === cs.length ? `${nTf} timeframes` : `${cs.length} ideas`;
    const coin = `<td class="left coin" rowspan="${span}"><a class="sym" href="#/coin/${encodeURIComponent(scope)}" title="Open the ${esc(scope)} coin page">${esc(scope)}</a>${sub ? `<span class="sub">${sub}</span>` : ""}</td>`;
    return cs.map((c, i) => ideaRow(c, i ? "" : coin, !i)).join("");
  }).join("");
  return head + `<tbody>${body}</tbody>`;
}

// The Strategist logs most ideas, so only other sources are named under the timeframe.
function ideaRow(c, coinCell, first) {
  const long = c.direction === "long", exp = state.openIdeas.has(c.id);
  const [crit, , critTip] = criticOf(c);
  let result, r;
  if (c.outcome === "graded") {
    result = `${esc(EXIT_LABEL[c.exit_reason] || humanize(c.exit_reason))}<span class="sub">${fmtPct(c.net_return_pct, 2)} net</span>`;
    r = rCell(c.r_multiple);
  } else if (c.outcome === "ungradeable") {
    result = `Ungradeable<span class="sub">${esc(UNGRADEABLE[c.exit_reason] || humanize(c.exit_reason))}</span>`;
    r = "–";
  } else {
    result = `Open<span class="sub">${c.outcome ? timeLeft(c.horizon_until) : "not marked yet"}</span>`;
    r = c.r_multiple == null ? "–" : `${rCell(c.r_multiple, "unreal")}<span class="sub">unrealised</span>`;
  }
  const cls = [c.status === "vetoed" && "vetoed", first && "g-first", exp && "expanded"].filter(Boolean).join(" ");
  return `<tr data-idea="${c.id}" class="${cls}" tabindex="0" aria-expanded="${exp}">${coinCell}
    <td class="left tf">${esc(c.tf)}<span class="side-m ${long ? "up" : "down"}" title="${long ? "Long" : "Short"}">${long ? "▲" : "▼"}</span>${c.source_id !== "strategist" ? `<span class="sub">${esc(srcLabel(c.source_id))}</span>` : ""}${c.parent_id ? `<span class="sub">revises #${c.parent_id}</span>` : ""}</td>
    <td class="left side-col"><span class="side ${long ? "up" : "down"}">${long ? "▲ Long" : "▼ Short"}</span></td>
    <td class="left wide">${ago(c.logged_at)}<span class="sub">${utc(c.logged_at).slice(5)}</span></td>
    <td class="wide">${fmtPrice(c.ref_price)}</td><td class="wide">${c.reward_risk == null ? "–" : c.reward_risk.toFixed(2)}</td>
    <td class="left"><span class="verdict ${esc(c.critique?.verdict || (c.status === "vetoed" ? "veto" : "none"))}" title="${esc(critTip)}">${crit}</span></td>
    <td class="left">${result}</td><td>${r}</td></tr>` + (exp ? ideaDetail(c) : "");
}

function ideaDetail(c) {
  const ev = c.evidence || {}, cr = c.critique, open = isOpenIdea(c);
  const move = (p) => (p == null || !c.ref_price ? "" : ` <span class="cat">${fmtPct((p / c.ref_price - 1) * 100, 1)}</span>`);
  const lvl = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  const plan = [
    lvl("Entry", `${fmtPrice(c.ref_price)} <span class="cat">our price when logged</span>`),
    lvl("Stop", fmtPrice(c.stop) + move(c.stop)),
    lvl((c.targets || []).length > 1 ? "Targets" : "Target", (c.targets || []).map((t) => fmtPrice(t) + move(t)).join("<br>") || "–"),
    lvl("Reward : risk", c.reward_risk == null ? "–" : c.reward_risk.toFixed(2)),
    lvl("Confidence", c.confidence == null ? "–" : `${Math.round(c.confidence * 100)}%`),
    lvl("Horizon", `${utc(c.horizon_until)}${open ? ` <span class="cat">${timeLeft(c.horizon_until)}</span>` : ""}`),
  ].join("");
  const now = c.outcome === "graded"
    ? `<p><b>Result:</b> ${esc(EXIT_LABEL[c.exit_reason] || humanize(c.exit_reason))} at ${fmtPrice(c.exit_price)}, ${fmtPct(c.net_return_pct, 2)} (${signed(c.r_multiple)}R) after fees, slippage and funding. Best ${fmtPct(c.mfe_pct, 2)} and worst ${fmtPct(c.mae_pct, 2)} while held.</p>`
    : c.outcome === "ungradeable" ? `<p><b>Not gradeable:</b> ${esc(UNGRADEABLE[c.exit_reason] || humanize(c.exit_reason))}.</p>`
    : c.net_return_pct != null ? `<p><b>Now:</b> ${fmtPct(c.net_return_pct, 2)}${c.r_multiple != null ? ` (${signed(c.r_multiple)}R)` : ""} if closed at the last candle, after exit costs. Best ${fmtPct(c.mfe_pct, 2)} and worst ${fmtPct(c.mae_pct, 2)} so far. <span class="cat">Updated ${ago(c.graded_at)}.</span></p>`
    : `<p class="cat">Not marked yet: the backtester checks every idea every 5 minutes.</p>`;
  return `<tr class="detail"><td colspan="${IDEA_COLS.length - 1}"><div class="idea-d">
    <dl class="idea-plan">${plan}</dl>${now}
    ${cr ? `<p><b>Critic, ${esc(criticOf(c)[1])}:</b> ${esc(cr.reason)}</p>` : `<p class="cat">${esc(criticOf(c)[2])}.</p>`}
    ${c.thesis ? `<p><b>Thesis:</b> ${esc(c.thesis)}</p>` : ""}
    ${ev.trigger && TRIGGER[ev.trigger.split(":")[0]] ? `<p><b>Why now:</b> the Oracle was woken by ${TRIGGER[ev.trigger.split(":")[0]]}.</p>` : ""}
    ${(ev.citations || []).length ? `<p><b>Evidence:</b></p><ul class="idea-cites">${ev.citations.map((x) => `<li>${esc(x.claim)}${/^https?:\/\//.test(x.url || "") ? ` <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ""}</li>`).join("")}</ul>` : ""}
    <p class="cat">Call #${c.id} by ${esc(srcLabel(c.source_id))}, logged ${utc(c.logged_at)}${c.parent_id ? `, revises #${c.parent_id}` : ""} · ${esc(c.market)} ${esc(c.entry_type)} entry · relies on ${esc((ev.relies_on || []).map(srcLabel).join(", ") || "–")}${ev.regime ? ` · regime ${esc(humanize(ev.regime))}` : ""}</p>
  </div></td></tr>`;
}

// ─── ideas backtest ────────────────────────────────────────────────────────
const ISC_VERDICT = { "beats baselines": "▲ Beats baselines", "behind baselines": "▼ Behind baselines", "within noise of baselines": "· Within noise of baselines" };
const CALL_STATUS = { proposed: ["Proposed", "let through by the critic"], vetoed: ["Vetoed", "blocked by the critic"] };
const rFmt = (v) => (v == null ? "–" : `${signed(v)}R`);
const minus = (a, b) => (a == null || b == null ? null : a - b);
const cap = (s) => s.replace(/^./, (c) => c.toUpperCase());
const ptsFmt = (v) => (v == null ? "–" : `${signed(v * 100, 1)} points`);

async function loadIdeaScorecard() {
  const byTf = state.iscSplit === "tf";
  const [tot, split] = await Promise.all([get("/api/ideas/scorecard?days=0&by_tf=false"), byTf ? get("/api/ideas/scorecard?days=0&by_tf=true") : null]);
  state.iscTotal = tot;
  state.isc = split || tot;
  renderIdeasAnswer();
  renderIdeaScorecard();
}

function criticSentence(c, min) {
  if (!c) return "";
  return c.verdict === "too few to judge the critic"
    ? `too few closed to judge it yet: ${c.proposed_n} let through and ${c.vetoed_n} vetoed ideas graded, ${min} of each needed.`
    : `${c.verdict}: ideas it let through averaged ${Math.abs(c.avg_r_diff).toFixed(2)}R ${c.avg_r_diff >= 0 ? "more" : "less"} than the ones it vetoed (band ±${c.band.toFixed(2)}R; ${c.proposed_n} vs ${c.vetoed_n} graded).`;
}

// "Are the ideas making money?": the ideas the critic let through, stop/target exits, all history.
function renderIdeasAnswer() {
  const d = state.iscTotal;
  if (!d) return;
  const P = "stop_target", min = d.min_n;
  const props = d.rows.filter((r) => r.policy === P && r.status === "proposed" && !r.call_type)
    .sort((a, b) => srcRank(a.source_id) - srcRank(b.source_id) || (b.n + b.n_open) - (a.n + a.n_open));
  if (!props.length) {
    $("#ans-ideas-body").innerHTML = `<p class="answer-verdict"><b>No ideas yet.</b> The Strategist logs one when its 1h/4h/1d call is strong and reward:risk clears 1.5; each is graded here net of costs.</p>`;
    return;
  }
  const stat = (k, v, s) => `<div><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  $("#ans-ideas-body").innerHTML = props.map((p) => {
    const o = d.open.find((x) => x.policy === P && x.status === "proposed" && x.source_id === p.source_id);
    const total = p.n + p.n_open + p.n_ungradeable, n = p.n, critiqued = CRITIC_SOURCES.has(p.source_id);
    const whose = `${total === 1 ? "idea" : "ideas"}${critiqued ? " the critic let through" : ""}`;
    let verdict;
    if (!n) {    // nothing to tabulate yet: one sentence, with where the open ones stand
      const now = o && o.avg_r != null ? ` ${o.n === 1 ? "The open one is" : `The ${o.n} open are`} at ${rCell(o.avg_r, "unreal")}${o.n === 1 ? "" : " on average"} if closed now (unrealised).` : "";
      return `${props.length > 1 ? `<h4 class="answer-src">${esc(srcLabel(p.source_id))}</h4>` : ""}<p class="answer-verdict"><b>Not known yet.</b> ${total === 1 ? `Its only ${whose} hasn't` : `None of the ${total} ${whose} has`} closed; results land as stops, targets and horizons are hit.${now}</p>`;
    }
    else if (n < min) verdict = `<b>Too early to tell.</b> ${n} of ${total} ${whose} ${n === 1 ? "has" : "have"} closed; a verdict needs ${min}.`;
    else {
      const yes = (p.avg_r ?? p.avg_net_return_pct) > 0;
      const vs = {
        "beats baselines": yes ? "and ahead of" : "though ahead of", "behind baselines": yes ? "but behind" : "and behind",
        "within noise of baselines": yes ? "but no better than" : "and no different from",
      }[p.vs_baselines];
      verdict = `<b>${yes ? "Yes" : "No"}:</b> ${rFmt(p.avg_r)} per idea on average after costs, ${vs || "compared with"} always-long and a coin flip on the same windows.`;
    }
    const wins = n ? Math.round(p.win_rate * n) : 0;
    const c = critiqued && d.critic.find((x) => x.policy === P && x.source_id === p.source_id);
    return `${props.length > 1 ? `<h4 class="answer-src">${esc(srcLabel(p.source_id))}</h4>` : ""}<p class="answer-verdict">${verdict}</p>
      <div class="answer-stats">
        ${stat("Closed", n, `of ${total} ${critiqued ? "let through" : "logged"}`)}
        ${stat("Average", n ? rCell(p.avg_r) : "–", n ? `${wins} of ${n} won` : "none closed yet")}
        ${stat("Always-long", n ? rCell(p.always_long.avg_r) : "–", "same windows and costs")}
        ${stat("Open now", o ? rCell(o.avg_r, "unreal") : "–", o ? `${o.n} open, unrealised` : "nothing open")}
      </div>
      ${c ? `<p class="answer-foot"><b>The critic:</b> ${esc(criticSentence(c, min))}</p>` : ""}`;
  }).join("") + `<p class="answer-foot">R is the net return as a multiple of the distance to the stop. Stop/target exits, all history. <button class="fold-link" data-openfold="isc-fold">The full backtest ↓</button></p>`;
}

function iscCells(r) {
  const base = (b, first) => `<td class="base${first ? " bl" : ""}">${pct(b.win_rate)}</td><td class="base">${rFmt(b.avg_r)}</td><td class="base">${fmtPct(b.avg_net_return_pct, 2)}</td>`;
  return `<td>${r.n}</td><td>${r.n_open}</td><td>${r.n_ungradeable}</td>
    <td class="bl">${pct(r.win_rate)}</td><td>${rFmt(r.avg_r)}${r.avg_r_stderr == null ? "" : ` <span class="cat">±${r.avg_r_stderr.toFixed(2)}</span>`}</td>
    <td>${rFmt(r.total_r)}</td><td>${fmtPct(r.avg_net_return_pct, 2)}</td>${base(r.always_long, true)}${base(r.random, true)}
    <td class="left why">${r.note ? `<span class="cat">${esc(r.note)}</span>` : esc(ISC_VERDICT[r.vs_baselines] || r.vs_baselines)}</td>`;
}

function renderIdeaScorecard() {
  const d = state.isc;
  if (!d) return;
  const rows = d.rows.filter((r) => r.policy === state.iscPolicy);
  const byTf = state.iscSplit === "tf";
  const cols = 15;
  const head = `<thead><tr><th class="left"></th><th colspan="3" class="grp-h">Ideas</th><th colspan="4" class="grp-h bl">Graded result</th>
    <th colspan="3" class="grp-h bl">Always-long, same windows</th><th colspan="3" class="grp-h bl">Random side, same windows</th><th></th></tr>
    <tr><th class="left">${byTf ? "Timeframe · calls" : "Calls"}</th><th>Graded</th><th>Open</th><th title="Could not be graded (no candles, no cost model, bad levels)">Ungr.</th>
    <th class="bl">Win rate</th><th title="Mean R multiple ± standard error">Avg R</th><th>Total R</th><th>Avg net</th>
    <th class="bl">Win rate</th><th>Avg R</th><th>Avg net</th><th class="bl">Win rate</th><th>Avg R</th><th>Avg net</th><th class="left">Verdict</th></tr></thead>`;
  const sources = [...new Set(rows.map((r) => r.source_id))].sort((a, b) => srcRank(a) - srcRank(b) || a.localeCompare(b));
  const critic = (src) => d.critic.find((x) => x.policy === state.iscPolicy && x.source_id === src);
  // The critic's verdict leads, above the table, so it is readable without scrolling sideways on a phone.
  $("#isc-critic").innerHTML = sources.filter((src) => CRITIC_SOURCES.has(src)).map((src) => {
    const c = critic(src);
    return c ? `<div><b>${esc(srcLabel(src))} critic:</b> ${esc(criticSentence(c, d.min_n))}</div>` : "";
  }).join("");
  $("#isc-critic").style.display = $("#isc-critic").innerHTML ? "" : "none";
  const tfOrder = (t) => TFS.indexOf(t);
  const body = sources.map((src) => {
    const mine = rows.filter((r) => r.source_id === src)
      .sort((a, b) => (byTf ? tfOrder(a.tf) - tfOrder(b.tf) : 0) || (a.status === b.status ? 0 : a.status === "proposed" ? -1 : 1));
    const lines = mine.map((r) => {
      const [label, sub] = CRITIC_SOURCES.has(src) ? CALL_STATUS[r.status] || [humanize(r.status), ""] : ["All ideas", "no critic reviews these"];
      return `<tr class="${r.status}"><td class="left">${byTf ? `<b>${esc(r.tf)}</b> · ` : ""}${esc(label)}${byTf || !sub ? "" : `<span class="sub">${sub}</span>`}</td>${iscCells(r)}</tr>`;
    }).join("");
    let diff = "";
    if (!byTf && CRITIC_SOURCES.has(src)) {   // how the critic is judged: did the ideas it let through beat the ones it blocked?
      const p = mine.find((r) => r.status === "proposed"), v = mine.find((r) => r.status === "vetoed");
      const c = critic(src);
      const few = !c || c.verdict === "too few to judge the critic";
      diff = `<tr class="diff${few ? " few" : ""}"><td class="left">Proposed − vetoed</td><td colspan="3"></td>
        <td class="bl">${ptsFmt(minus(p?.win_rate, v?.win_rate))}</td><td>${rFmt(c?.avg_r_diff)}${c?.band == null ? "" : ` <span class="cat">±${c.band.toFixed(2)}</span>`}</td>
        <td></td><td>${fmtPct(minus(p?.avg_net_return_pct, v?.avg_net_return_pct), 2)}</td>
        <td colspan="6" class="bl"></td><td class="left why">${esc(c ? cap(c.verdict) : "No proposed ideas")}</td></tr>`;
    }
    return `<tr class="grp"><td colspan="${cols}">${esc(srcLabel(src))}</td></tr>${lines}${diff}`;
  }).join("");
  $("#isc").innerHTML = head + `<tbody>${body || `<tr><td colspan="${cols}" class="left">Nothing graded yet. The backtester checks every idea every 5 minutes and grades it when its stop, target or horizon is reached.</td></tr>`}</tbody>`;
  const all = rows.filter((r) => !byTf || r.tf);
  const sum = (k) => all.reduce((a, r) => a + (r[k] || 0), 0);
  $("#isc-note").textContent = `${sum("n")} graded, ${sum("n_open")} open, ${sum("n_ungradeable")} ungradeable. ` +
    "Win = net return above zero after fees, slippage and funding; R = net return ÷ the distance to the stop. " +
    "Baselines use each idea's own entry, exit and costs: always-long, and a coin-flip side (which loses a little on average because of costs). " +
    `Verdicts compare avg R with the better baseline using a rough 95% band (±1.96 × stderr); nothing is judged below ${d.min_n} graded. ` +
    "Proposed − vetoed is how the critic is judged: positive means its vetoes avoided worse trades.";
}

// ─── reports ───────────────────────────────────────────────────────────────
const REPORT_KIND = { brief: "4-hour brief", daily: "Daily wrap", oracle_daily: "Oracle thesis", oracle_weekly: "Oracle weekly review", ceo: "CEO memo" };
const REPORT_CHIP = { brief: "Briefs", daily: "Daily wraps", oracle_daily: "Oracle", oracle_weekly: "Weekly reviews", ceo: "CEO memos" };
const REPORT_HOURS = { brief: 4, daily: 24 };            // the window each one covers (agents/reports.py WINDOWS)
const reportKind = (k) => REPORT_KIND[k] || humanize(k);
// Briefs are written at :50 every 4 hours from 00:50 UTC (albatross/__main__.py).
function nextBrief() {
  const now = new Date(), t = new Date(now);
  t.setUTCHours(Math.floor(t.getUTCHours() / 4) * 4, 50, 0, 0);
  while (t <= now) t.setUTCHours(t.getUTCHours() + 4);
  return t;
}

async function loadReports() {
  const list = await get("/api/reports?limit=60");
  if (!list.some((r) => r.id === state.reportId)) {        // an unknown or missing id opens the newest
    state.reportId = list[0]?.id ?? null;
    if (state.tab === "reports") history.replaceState(null, "", `#${route()}`);
  }
  const kinds = [...new Set(list.map((r) => r.kind))];
  if (!kinds.includes(state.reportKind)) state.reportKind = "";
  const chips = $("#report-kinds");
  chips.hidden = kinds.length < 2;
  chips.innerHTML = [["", "All"], ...kinds.map((k) => [k, REPORT_CHIP[k] || reportKind(k)])]
    .map(([k, t]) => `<button data-rkind="${esc(k)}" class="${k === state.reportKind ? "on" : ""}">${esc(t)}</button>`).join("");
  $("#report-list").innerHTML = list.filter((r) => !state.reportKind || r.kind === state.reportKind).map((r) =>
    `<li><button data-report="${r.id}" class="${r.id === state.reportId ? "on" : ""}" title="${utc(r.created_at)}">
      <span class="rkind">${esc(reportKind(r.kind))}</span><span class="cat"> · ${ago(r.created_at)}</span><span class="rtitle">${esc(r.title)}</span></button></li>`).join("");
  const next = nextBrief(), mins = Math.round((next - Date.now()) / 60000);
  $("#report-next").textContent = `Next brief at ${hhmm(next)}, in ${mins < 90 ? `${mins} min` : `${Math.round(mins / 60)} h`}.`;
  $("#tab-reports .reports").classList.toggle("single", list.length < 2);
  $("#tab-reports .reports").classList.toggle("none", !list.length);
  const cur = $("#report");
  if (state.reportId == null) {
    cur.dataset.id = "";
    cur.innerHTML = `<div class="r-empty"><h2>No reports <em>yet</em>.</h2><p>The desk writes a brief at :50 past 00, 04, 08, 12, 16 and 20 UTC,
      and a daily wrap straight after the 00:50 one. The first lands at ${hhmm(next)}.</p></div>`;
    return;
  }
  if (cur.dataset.id === String(state.reportId)) return;   // already showing it; keep the scroll position
  const r = await get(`/api/reports/${state.reportId}`);
  if (r.id !== state.reportId) return;                      // another report was picked while this loaded
  cur.dataset.id = r.id;
  const i = list.findIndex((x) => x.id === r.id), hours = REPORT_HOURS[r.kind];
  const step = (x, older) => (x ? `<a href="#/report/${x.id}" data-report="${x.id}" class="${older ? "older" : "newer"}">
    <span>${older ? "← Older" : "Newer →"}</span>${esc(x.title)}</a>` : "<span></span>");
  cur.innerHTML = `<header class="r-head"><p class="kicker">${esc(reportKind(r.kind))}</p><h2>${esc(r.title)}</h2>
      <p class="cat">${utc(r.created_at)} · ${ago(r.created_at)}${hours ? ` · covers the ${hours} hours before` : ""}</p></header>
    <div class="r-body">${md(r.body_md)}</div>
    ${list.length > 1 ? `<nav class="r-nav" aria-label="Other reports">${step(list[i + 1], true)}${step(list[i - 1], false)}</nav>` : ""}`;
  if (cur.getBoundingClientRect().top < 0) cur.scrollIntoView({ block: "start" });
}

// Markdown for LLM-written reports. Everything is escaped first; only headings, paragraphs, lists, block
// quotes, pipe tables, bold/italic, inline code and http(s) links are turned back into markup.
function md(src) {
  const out = [], lines = esc(String(src ?? "").replace(/\0/g, "")).split(/\r?\n/);
  let para = [], list = null, quote = [];
  const flush = () => {
    if (para.length) out.push(`<p>${mdInline(para.join(" "))}</p>`);
    if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${mdInline(i)}</li>`).join("")}</${list.tag}>`);
    if (quote.length) out.push(`<blockquote><p>${mdInline(quote.join(" "))}</p></blockquote>`);
    para = []; list = null; quote = [];
  };
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
  for (let n = 0; n < lines.length; n++) {
    const line = lines[n];
    let m;
    if (!line.trim()) flush();
    else if (line.includes("|") && RULE.test(lines[n + 1] || "")) {          // pipe table: header row, rule, rows
      flush();
      const head = cells(line), rows = [];
      for (n += 2; n < lines.length && lines[n].includes("|") && lines[n].trim(); n++) rows.push(cells(lines[n]));
      n--;
      const num = (c) => /^[-+−]?[$€]?[\d.,]+\s?(%|x|[KMB]|R|pts?|points|bps)?$/i.test(c);
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${mdInline(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${head.map((_, j) => `<td${num(r[j] ?? "") ? ' class="num"' : ""}>${mdInline(r[j] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    }
    else if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)[\s#]*$/))) { flush(); const h = Math.min(6, m[1].length + 1); out.push(`<h${h}>${mdInline(m[2])}</h${h}>`); }
    else if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); }
    else if ((m = line.match(/^\s{0,3}&gt;\s?(.*)$/))) { if (!quote.length) flush(); quote.push(m[1]); }
    else if ((m = line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.*)$/))) {
      const tag = m[1] ? "ul" : "ol";
      if (para.length || quote.length || list?.tag !== tag) flush();
      list ??= { tag, items: [] };
      list.items.push(m[2]);
    } else if (list && /^\s/.test(line)) list.items[list.items.length - 1] += ` ${line.trim()}`;
    else if (quote.length) quote.push(line.trim());                            // a lazy continuation line
    else { if (list) flush(); para.push(line.trim()); }
  }
  flush();
  return out.join("");
}

function mdInline(s) {
  const held = [];
  const hold = (html) => `\0${held.push(html) - 1}\0`;
  const emph = (t) => t.replace(/\*\*(?=\S)(.+?)\*\*|(^|[^\w])__(?=\S)(.+?)__(?!\w)/g, (_, a, pre, b) => (a != null ? `<strong>${a}</strong>` : `${pre}<strong>${b}</strong>`))
    .replace(/\*(?=\S)(.+?)\*|(^|[^\w])_(?=\S)(.+?)_(?!\w)/g, (_, a, pre, b) => (a != null ? `<em>${a}</em>` : `${pre}<em>${b}</em>`));
  // Code spans and links are set aside first so emphasis can't reach inside them (URLs contain _ and *).
  s = s.replace(/`([^`]+)`/g, (_, c) => hold(`<code>${c}</code>`));
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, text, url) =>
    /^https?:\/\//i.test(url) ? hold(`<a href="${url}" target="_blank" rel="noopener noreferrer">${emph(text)}</a>`) : text);
  // Older reports quote snake_case labels ("pullback_in_uptrend"); in prose they read as words.
  s = s.replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, (w) => w.replaceAll("_", " "));
  return emph(s).replace(/\0(\d+)\0/g, (_, i) => held[i]);
}
// ─── coin page (the drill-down under Markets) ─────────────────────────────
function openAsset(sym) {
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true");
  if (state.tab !== "coin") state.coinFrom = state.tab;
  state.coin = String(sym).toUpperCase();
  setTab("coin");
}

// One source's live call per timeframe, and the tooltip every coin-page cell shares.
const callsBy = (views, src) => Object.fromEntries(TFS.map((t) => [t, (views[t] || []).find((v) => v.source === src)]));
const viewTip = (v, t) => `<b>${esc(srcLabel(v.source))} · ${t}</b>` + row("Score", signed(v.score)) + row("Called", utc(v.as_of)) +
  row("Valid until", utc(v.valid_until)) + whyHtml(v);

async function renderCoin(sym) {
  const body = $("#coin-body");
  if (body.dataset.sym !== sym) {       // another coin: never leave the last one's numbers under this title
    body.dataset.sym = sym;
    body.innerHTML = `<p class="note coin-wait">Loading ${esc(sym)}…</p>`;
  }
  let d;
  try {
    d = await get(`/api/asset/${encodeURIComponent(sym)}`);
  } catch (e) {
    if (state.coin !== sym || state.tab !== "coin") return;
    // Failures are shown by the shared loader guard; an unknown symbol is an answer, not a failure.
    if (!e.message.endsWith(": 404")) {
      if ($(".coin-wait", body)) body.innerHTML = "";      // nothing of this coin to keep
      throw e;
    }
    body.innerHTML = `<article class="panel coin-empty"><h2>No coin called ${esc(sym)}</h2><p>Albatross doesn't track that symbol.</p>
      <a class="btn-paint" href="#/matrix" data-go="matrix">Browse every coin</a></article>`;
    document.dispatchEvent(new CustomEvent("coinloaded", { detail: { symbol: sym, missing: true } }));
    return;
  }
  if (state.coin !== sym || state.tab !== "coin") return;      // navigated away while loading
  const a = d.asset, f = d.features, n = d.candles_1h.length;
  // The strip shows the source picked on All coins (the Strategist unless changed); a coin that source
  // doesn't cover falls back to the quant model, and says so.
  const pref = state.matrix?.source || "strategist";
  const has = (src) => TFS.some((t) => (d.views[t] || []).some((v) => v.source === src));
  const src = has(pref) ? pref : has("quant") ? "quant" : null;
  // The row clicked on All coins, when it is this source, so the strip matches it (stale calls included).
  const m = src === state.matrix?.source ? state.matrix.assets.find((x) => x.symbol === sym) : null;
  const calls = m ? m.tfs : src ? callsBy(d.views, src) : {}, st = callsBy(d.views, "strategist");
  const reg = st["4h"]?.regime || st["1h"]?.regime || (src ? regime(calls) : null);
  const tfs = TFS.filter((t) => d.views[t]?.length || calls[t]), ftfs = TFS.filter((t) => f[t]);
  const why = TFS.map((t) => [t, st[t]]).filter(([, v]) => v);
  const unc = src === "quant" ? {} : d.uncovered || {};      // the quant model's own view shows every timeframe
  const stripNote = src === pref ? `${esc(srcLabel(src))} · hover a cell for its reasoning`
    : `Quant model · ${pref === "strategist" && a.tier > 2 ? "the Strategist covers tier 1 and 2 coins" : `no ${esc(srcLabel(pref))} call on this coin`}`;
  const metrics = [
    ["Close", (x) => fmtPrice(x.close)], ["Structure", (x) => x.structure], ["EMA stack", (x) => x.ema_stack],
    ["RSI 14", (x) => x.rsi14?.toFixed(1) ?? "–"], ["vs EMA50", (x) => fmtPct(x.dist_ema50_pct)], ["vs EMA200", (x) => fmtPct(x.dist_ema200_pct)],
    ["ATR", (x) => fmtPct(x.atr_pct, 2)], ["Volume z", (x) => x.vol_z?.toFixed(2) ?? "–"],
    ["Swing high", (x) => fmtPrice(x.last_swing_high)], ["Swing low", (x) => fmtPrice(x.last_swing_low)],
  ];
  const levels = f["1d"] ? [{ v: f["1d"].last_swing_high, label: "1d swing high" }, { v: f["1d"].last_swing_low, label: "1d swing low" }] : [];
  // The minute refresh redraws the page: keep open whichever folds were open on this coin.
  const open = new Set([...body.querySelectorAll("details[data-k][open]")].map((x) => x.dataset.k));
  const fold = (k, title, html) => `<details class="fold" data-k="${k}"${open.has(k) ? " open" : ""}><summary>${title}</summary>${html}</details>`;
  const recent = d.news.filter((x) => Date.now() - new Date(x.published_at) < 30 * 864e5);   // older headlines are history, not news
  const news = `<article class="panel"><header class="panel-h"><h2>News</h2><span class="cat">tagged ${esc(sym)}, last 30 days</span></header>
      <ul class="news bare">${recent.slice(0, 8).map(newsItem).join("") || `<li class="empty">No headlines tagged ${esc(sym)} in the last 30 days.</li>`}</ul></article>`;
  // The Reddit scout's latest read of this coin (it reads tier 1-2 coins with big news), when it has one.
  const r = d.reddit, rlink = (u) => (/^https:\/\/(www\.|old\.)?reddit\.com\//.test(u || "") ? u : null);
  const reddit = !r ? "" : `<article class="panel coin-reddit"><header class="panel-h"><h2>Reddit</h2><span class="cat">${
      r.found ? `${esc(r.mood_label || "")}${r.activity_label ? ` · ${esc(r.activity_label.toLowerCase())}` : ""}` : "no threads returned"} · ${esc(ago(r.as_of))}</span></header>
    ${(r.red_flags || []).map((f) => { const u = rlink(f.url); return `<p class="rd-flag"><b>${esc(f.kind)}</b> ${esc(f.detail)}${u ? ` <a href="${esc(u)}" target="_blank" rel="noopener noreferrer">thread ↗</a>` : ""}</p>`; }).join("")}
    ${r.found ? `${r.summary ? `<p class="rd-say">${esc(r.summary)}</p>` : ""}${r.why_it_matters ? `<p class="xs-why">${esc(r.why_it_matters)}</p>` : ""}
      <ul class="rd-threads">${(r.threads || []).slice(0, 5).map((t) => { const u = rlink(t.url); return `<li><span class="cat">${esc(t.subreddit || "")}</span> ${
        u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t.title)} ↗</a>` : esc(t.title)}${t.posted ? ` <span class="cat">${esc(t.posted)}</span>` : ""}</li>`; }).join("")}</ul>`
      : `<p class="note">Our searches returned no Reddit thread about ${esc(sym)}.</p>`}
    <p class="note">Read through Google search: each thread is one Google's own link led to.</p></article>`;
  body.innerHTML = !src && n < 2 && !ftfs.length
    ? `<article class="panel coin-empty"><p>Albatross tracks ${esc(sym)} but has no price history or calls for it yet.</p></article>${news}${reddit}`
    : `${src ? `<article class="panel"><header class="panel-h"><h2>Bias</h2><span class="cat">${stripNote}</span></header>
      <div class="table-wrap bare"><table class="plain bias-strip"><thead><tr>${tfs.map((t) => `<th>${t}</th>`).join("")}</tr></thead>
      <tbody><tr>${tfs.map((t) => (unc[t] && !calls[t] ? noSkillCell(t, unc[t]) : m ? cellHtml(calls[t], `${sym}|${t}`) : cellHtml(calls[t], null, calls[t] && viewTip(calls[t], t)))).join("")}</tr></tbody></table></div>
      ${tfs.some((t) => unc[t]) ? `<p class="note">${esc(uncoveredNote(unc).trim())}</p>` : ""}
      ${src === "quant" && src === state.matrix?.source && state.matrix.source_rung ? `<p class="note">${esc(benchedNote(state.matrix).trim())}</p>` : ""}</article>` : ""}
    <div class="coin-grid${why.length ? "" : " solo"}">
      ${why.length ? `<article class="panel"><header class="panel-h"><h2>Why</h2><span class="cat">the Strategist, per timeframe</span></header>
        <div class="table-wrap bare"><table class="plain why-t"><tbody>${why.map(([t, v]) => `<tr><td class="tf">${t}</td>${cellHtml(v, null, viewTip(v, t))}
          <td class="why left">${esc(v.thesis || "No rationale recorded.")}</td></tr>`).join("")}</tbody></table></div></article>` : ""}
      <article class="panel"><header class="panel-h"><h2>Price</h2><span class="cat">${n >= 2 ? `hourly closes, last ${n >= 48 ? `${Math.round(n / 24)} days` : `${n} hours`}` : ""}</span></header><div id="chart"></div></article>
    </div>
    ${tfs.length ? fold("views", "Every source's call, with its weight", viewsTable(d.views)) : ""}
    ${ftfs.length ? fold("ind", "Indicators by timeframe", `<div class="table-wrap"><table class="plain"><thead><tr><th></th>${ftfs.map((t) => `<th>${t}</th>`).join("")}</tr></thead><tbody>
      ${metrics.map(([k, fn]) => `<tr><td>${k}</td>${ftfs.map((t) => `<td>${esc(fn(f[t]) ?? "–")}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>`) : ""}
    ${news}${reddit}`;
  const chart = body.querySelector("#chart");
  if (chart) {
    if (n < 2) chart.innerHTML = `<p class="note">No price history collected for ${esc(sym)} yet.</p>`;
    else drawChart(chart, d.candles_1h.map((c) => ({ t: new Date(c.ts), v: c.close })), levels);
  }
  // The price and 24h move All coins shows for this coin (the last hourly close when it has none).
  const listed = state.matrix?.assets.find((x) => x.symbol === sym);
  document.dispatchEvent(new CustomEvent("coinloaded", { detail: {
    symbol: a.symbol, name: a.name, tier: a.tier, mcap: a.market_cap_usd,
    price: listed?.price ?? d.candles_1h[n - 1]?.close, chg: listed?.chg_24h_pct,
    categories: d.categories.map((c) => c.name), regime: reg } }));
}

// Per timeframe: the Strategist's final call, the ensemble, then each source by its ensemble weight.
function viewsTable(views) {
  const tfs = TFS.filter((t) => views[t]?.length);
  if (!tfs.length) return `<p class="note">No live calls for this asset.</p>`;
  const rank = { strategist: 0, ensemble: 1 };
  const body = tfs.map((t) => {
    const vs = [...views[t]].sort((a, b) => (rank[a.source] ?? 2) - (rank[b.source] ?? 2) || (b.weight ?? 0) - (a.weight ?? 0));
    const st = vs.find((v) => v.source === "strategist");
    const head = `<tr class="grp"><td colspan="5"><b>${t}</b>${st?.regime ? ` <span class="cat">· ${esc(humanize(st.regime))}</span>` : ""}</td></tr>`;
    return head + vs.map((v) => {
      const ens = v.source === "ensemble", fin = v.source === "strategist";
      const conf = v.confidence == null ? "–" : `${ens ? "agr " : ""}${Math.round(v.confidence * 100)}%`;
      const wTip = v.weight == null ? null : `<b>${esc(srcLabel(v.source))} · ${t} weight ${v.weight.toFixed(2)}</b>` +
        row("Graded calls (30d)", v.graded ?? 0) + row("Edge vs one-way", v.edge == null ? "not graded yet" : `${signed(v.edge * 100, 1)} points`);
      const why = ens
        ? `score ${signed(v.score)} from ${Object.keys(v.parts || {}).map(srcLabel).join(", ") || "–"}`
        : `${esc(v.thesis || "")}${v.relies_on?.length ? `<span class="sub">relies on ${esc(v.relies_on.map(srcLabel).join(", "))}</span>` : ""}`;
      return `<tr class="${fin ? "final" : ""}"><td class="left">${esc(srcLabel(v.source))}${fin ? ` <span class="cat">final</span>` : ""}</td>
        ${cellHtml(v, null, `<b>${esc(srcLabel(v.source))} · ${t}</b>` + row("Score", signed(v.score)) + row("Called", utc(v.as_of)) + row("Valid until", utc(v.valid_until)) + whyHtml(v))}
        <td>${conf}</td><td${wTip ? ` data-tipid="${tipId(wTip)}"` : ""}>${v.weight == null ? "–" : `${v.weight.toFixed(2)}×`}</td>
        <td class="why">${why}</td></tr>`;
    }).join("");
  }).join("");
  return `<div class="table-wrap"><table class="plain views"><thead><tr><th class="left">Source</th><th>Bias</th><th>Conf.</th>
    <th title="Current weight in the blended call, from the source's measured edge over a one-way caller on this timeframe">Weight</th><th class="left">Thesis / rationale</th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

// Round-number gridlines (80,000 / 82,000 …), three to five across the range.
function niceTicks(lo, hi, n = 5) {
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw);
  const out = [];
  for (let i = Math.ceil(lo / step); i * step <= hi; i++) out.push(i * step);
  return out;
}
const shortUtc = (t) => new Date(t).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC" });

function drawChart(host, pts, levels, opt = {}) {
  const fmt = opt.fmt || fmtPrice, tickFmt = opt.fmt || ((v) => fmtPrice(v).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ""));
  if (pts.length < 2) { host.innerHTML = `<p class="note">${opt.lo != null ? "Only one read so far; the line appears after the next." : "Not enough candles yet."}</p>`; return; }
  const W = host.clientWidth || 640, H = 220, L = 8, R = 64, T = 10, B = 22;
  const vals = pts.map((p) => p.v);
  const visible = levels.filter((l) => l.v != null);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || hi * 0.01;
  lo -= span * 0.05; hi += span * 0.05;
  if (opt.lo != null) { lo = opt.lo; hi = opt.hi; }
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const ticks = opt.lo != null ? [0, 0.25, 0.5, 0.75, 1].map((k) => lo + k * (hi - lo)) : niceTicks(lo, hi);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  // A level's label goes at whichever end the line is further from, so the price never runs through it.
  const gap = (l, part) => Math.min(...part.map((p) => Math.abs(p.v - l.v)));
  const q = Math.max(1, Math.floor(pts.length / 3));
  const lvl = visible.filter((l) => l.v >= lo && l.v <= hi).map((l) => {
    const right = l.side ? l.side === "right" : gap(l, pts.slice(-q)) >= gap(l, pts.slice(0, q));
    return `<line class="level" x1="${L}" x2="${W - R}" y1="${y(l.v)}" y2="${y(l.v)}" stroke="var(--muted)"/><text class="axis-label" x="${right ? W - R - 4 : L + 4}" y="${y(l.v) - 4}"${right ? ` text-anchor="end"` : ""}>${esc(l.label)} ${fmt(l.v)}</text>`;
  }).join("");
  host.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${opt.aria || (opt.lo != null ? "X mood over time" : "1 hour closing price")}">
    ${ticks.map((v) => `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-label" x="${W - R + 6}" y="${y(v) + 4}">${tickFmt(v)}</text>`).join("")}
    <text class="axis-label" x="${L}" y="${H - 6}">${opt.dateFmt ? opt.dateFmt(pts[0].t) : shortUtc(pts[0].t)}</text>
    <text class="axis-label" x="${W - R}" y="${H - 6}" text-anchor="end">${opt.dateFmt ? opt.dateFmt(pts[pts.length - 1].t) : `${shortUtc(pts[pts.length - 1].t)} UTC`}</text>
    ${lvl}<path class="series" d="${path}"/>
    <line class="cross" id="cx" y1="${T}" y2="${H - B}" visibility="hidden"/><circle class="dot" id="cd" r="4" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hit"/></svg>`;
  const svg = host.querySelector("svg"), cx = $("#cx", svg), cd = $("#cd", svg);
  $("#hit", svg).addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - L) / (W - L - R)) * (pts.length - 1))));
    cx.setAttribute("x1", x(i)); cx.setAttribute("x2", x(i)); cx.setAttribute("visibility", "visible");
    cd.setAttribute("cx", x(i)); cd.setAttribute("cy", y(pts[i].v)); cd.setAttribute("visibility", "visible");
    showTip(e, `<b>${fmt(pts[i].v)}</b>${row(opt.label || "Hour", opt.dateFmt ? opt.dateFmt(pts[i].t) : utc(pts[i].t))}`);
  });
  $("#hit", svg).addEventListener("pointerleave", () => { cx.setAttribute("visibility", "hidden"); cd.setAttribute("visibility", "hidden"); hideTip(); });
}

// ─── about ─────────────────────────────────────────────────────────────────
function openAbout() {
  $("#drawer-body").innerHTML = `
    <div class="about">
      <h2>Albatross</h2>
      <img class="about-art" src="/static/brand/albatross-painting.jpg" alt="Painting of an albatross on a rock in a storm" loading="lazy">
      <p>A 24/7 desk of agents that researches crypto, from the largest coins to memecoins called in Telegram groups. Nothing trades: every call is paper-traded and graded after costs.</p>
      <p>Every call and idea is written to an append-only, hash-chained ledger before its outcome exists, so the record can't flatter itself.</p>
      <h3>Reading the direction calls</h3>
      <table class="about-key"><tbody>
        <tr>${cellHtml({ bias: 2, score: 1, valid_until: "9999-01-01" })}${cellHtml({ bias: 1, score: 0.5, valid_until: "9999-01-01" })}<td>Blue leans long; 2 is a strong call.</td></tr>
        <tr>${cellHtml({ bias: -2, score: -1, valid_until: "9999-01-01" })}${cellHtml({ bias: -1, score: -0.5, valid_until: "9999-01-01" })}<td>Crimson leans short.</td></tr>
        <tr>${cellHtml({ bias: 0, score: 0, valid_until: "9999-01-01" })}${cellHtml({ bias: 1, score: 0.5, valid_until: "2000-01-01" })}<td>0 is no edge. Faded italics: the call has expired and a new one is due.</td></tr>
      </tbody></table>
      <h3>Where things are</h3>
      <dl class="about-map">
        <dt>Home</dt><dd>Is it working: the answer from the paper books, every book's standing, the CEO memo's actions with buttons, and the dates that matter.</dd>
        <dt>Alpha</dt><dd>Live: every memecoin call marked skip or pass as it lands, with its result a day later, and this week's factor lists. Then the review of what produces alpha and what doesn't, loss avoidance and the factor edge.</dd>
        <dt>Results</dt><dd>Every paper book in dollars, every source's record, whether the dossier picks winners, the ideas ledger, the reports and the strategy lab.</dd>
        <dt>Memecoins</dt><dd>The feed of memecoins called in the groups, and a dossier per token with the evidence in one place.</dd>
        <dt>Majors</dt><dd>Factor rank, every coin and sector, the calendar, news and X. Direction calls are marked unproven: none beats a coin flip yet.</dd>
        <dt>Ops</dt><dd>Agents, spend and the ChatGPT allowance, health and alerts, and every token we price.</dd>
      </dl>
    </div>`;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}

// ─── navigation: the sections, each with its views; drill-downs are views too ──
// The redesign (2026-10-06): Home answers "is it working"; Results holds every record; Memecoins the Deep End
// (deep.js); Majors factor rank first and the direction calls marked unproven; Ops runs the machine, in its own look.
const SECTIONS = {
  home: { views: ["home"] },
  alpha: { views: ["live", "review", "avoid", "edge"] },
  results: { views: ["books", "scorecard", "dreport", "ideas", "reports", "lab"], drill: ["book"] },
  memecoins: { views: ["deep"], drill: ["dossier"] },
  majors: { views: ["factor", "matrix", "sectors", "calendar", "news", "social"], drill: ["coin"] },
  ops: { views: ["agents", "output", "health", "micro", "setup"] },
};
const VIEW_NAME = {
  home: "Home", books: "Books", scorecard: "Sources", dreport: "Dossier report", ideas: "Ideas ledger", reports: "Reports",
  lab: "Strategy lab", live: "Live", review: "Review", avoid: "Loss avoidance", edge: "Factor edge",
  book: "Book", deep: "Feed", dossier: "Dossier", factor: "Factor rank", matrix: "All coins", sectors: "Sectors",
  calendar: "Calendar", news: "News", social: "X", coin: "Coin", agents: "Agents", output: "Spend & allowance",
  health: "Health & alerts", micro: "All tokens", setup: "Setup",
};
const sectionOf = (v) => Object.keys(SECTIONS).find((k) => SECTIONS[k].views.includes(v) || SECTIONS[k].drill?.includes(v)) || "home";
const lastView = {};            // a section reopens on the view it was left on
const catName = (slug) => state.sectors?.sectors.find((x) => x.slug === slug)?.name
  || state.matrix?.sector_names?.[slug] || humanize(slug);

// The hash names the current view (and drill), so Back works and any view can be linked. Routes start
// with "/" so they never match an element id (the browser would scroll to it and open its <details>).
function route() {
  if (state.tab === "coin") return `/coin/${encodeURIComponent(state.coin)}`;
  if (state.tab === "dossier") return `/deep/${encodeURIComponent(state.deep.chain)}/${encodeURIComponent(state.deep.address)}`;
  if (state.tab === "book") return `/book/${encodeURIComponent(state.book)}`;
  if (state.tab === "matrix" && state.cat) return `/sector/${encodeURIComponent(state.cat)}`;
  if (state.tab === "reports" && state.reportId) return `/report/${state.reportId}`;
  if (state.tab === "micro" && location.hash.startsWith("#micro=")) return location.hash.slice(1);
  return `/${state.tab}`;
}

function setTab(name, opt = {}) {
  if (!document.getElementById(`tab-${name}`) || (name === "coin" && !state.coin) || (name === "dossier" && !state.deep?.address)
    || (name === "book" && !state.book)) name = "home";
  const changed = name !== state.tab;
  state.tab = name;
  if (changed) { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); }   // a drawer belongs to its view
  const sec = sectionOf(name);
  if (SECTIONS[sec].views.includes(name)) lastView[sec] = name === "matrix" && state.cat ? "sectors" : name;
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.section === sec);
    b.toggleAttribute("aria-current", b.dataset.section === sec);
  });
  document.body.classList.toggle("ops-mode", sec === "ops");       // Ops has its own plainer look
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${name}`));
  renderSubnav();
  // a drill-down's entry keeps where it came from, so Back and Forward show the same trail
  const h = `#${route()}`, hs = name === "coin" ? { from: state.coinFrom || null } : name === "dossier" ? { from: state.deepFrom || null } : null;
  if (location.hash !== h) opt.replace ? history.replaceState(hs, "", h) : history.pushState(hs, "", h);
  else if (hs && history.state?.from !== hs.from) history.replaceState(hs, "", h);    // a plain link's own entry
  try { localStorage.setItem("albatross.tab", name === "coin" ? "matrix" : name === "dossier" ? "deep" : name === "book" ? "books" : name); } catch {}
  document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: name, section: sec } }));
  if (changed || name === "coin" || name === "dossier" || name === "book") scrollTo({ top: 0 });
  refresh();
}

function renderSubnav() {
  const sec = sectionOf(state.tab), views = SECTIONS[sec].views, nav = $("#subnav");
  // A drill-down replaces the section's segment control with the trail back up: [view, label, keep sector].
  let trail = null;
  if (state.tab === "coin") {
    const from = state.coinFrom && state.coinFrom !== "coin" ? state.coinFrom : "matrix";
    trail = from === "matrix" && state.cat ? [["sectors", "Sectors"], ["matrix", catName(state.cat), true]]
      : [[from, from === "social" ? "X posts" : VIEW_NAME[from]]];        // a bare "X ›" reads like a close button
    trail.push([null, state.coin]);
  } else if (state.tab === "dossier") {
    const from = state.deepFrom === "micro" ? "micro" : "deep", a = state.deep.address;
    trail = [[from, VIEW_NAME[from]], [null, state.deep.label || (a.length > 14 ? `${a.slice(0, 5)}…${a.slice(-4)}` : a)]];
  } else if (state.tab === "matrix" && state.cat) {
    trail = [["sectors", "Sectors"], [null, catName(state.cat)]];
  } else if (state.tab === "book") {
    trail = [["books", "Books"], [null, state.bookName || humanize(state.book)]];
  }
  if (trail) {
    nav.innerHTML = `<div class="crumbs">${trail.map(([v, label, keep]) => (v
      ? `<a href="#/${keep ? `sector/${encodeURIComponent(state.cat)}` : v}" data-view="${v}"${keep ? ` data-keepcat="1"` : ""}>${esc(label)}</a>`
      : `<b aria-current="page">${esc(label)}</b>`)).join(`<span aria-hidden="true">›</span>`)}
      ${state.tab === "matrix" ? `<button class="clear" data-clearcat>show every coin</button>` : ""}</div>`;
  } else {
    nav.innerHTML = views.length > 1 ? `<div class="chips seg">${views.map((v) =>
      `<button data-view="${v}" class="${v === state.tab ? "on" : ""}"${v === state.tab ? ` aria-current="page"` : ""}>${VIEW_NAME[v]}</button>`).join("")}</div>` : "";
  }
  nav.hidden = !nav.innerHTML;
}

function setCat(slug) {
  state.cat = slug;
  const sel = $("#cat-filter");
  if (sel && [...sel.options].some((o) => o.value === slug)) sel.value = slug;
}
function openSector(slug) { setCat(slug); setTab("matrix"); }

function fromHash(h, replace, back) {           // back: the history entry's state, on Back and Forward
  h = String(h || "").replace(/^#\/?/, "");                          // "#/coin/BTC" and the older "#asset=BTC" both work
  try { h = decodeURIComponent(h); } catch { /* a malformed %-escape: route on the raw text */ }
  const arg = (...keys) => { for (const k of keys) if (h.startsWith(k)) return h.slice(k.length); return null; };
  let v;
  if ((v = arg("coin/", "asset=")) != null) {
    // A plain #/coin/ link (Ideas) gets the trail back a click gets; a fresh deep link keeps All coins; Back and
    // Forward bring back the trail the page had (setTab keeps it in the history entry).
    if (back && "from" in back) state.coinFrom = back.from;
    else if (state.tab !== "coin" && refreshedAt) state.coinFrom = state.tab;
    state.coin = v.toUpperCase(); setTab("coin", { replace }); return true;
  }
  if ((v = arg("deep/")) != null) {
    // #/deep/<chain>/<address>: a token's dossier. A click from the feed or the token list set where it came from.
    const [chain, ...rest] = v.split("/"), address = rest.join("/");
    if (!chain || !address) { setTab("deep", { replace }); return true; }
    if (state.deep?.chain !== chain || state.deep?.address !== address) state.deep = { chain, address };
    if (back && "from" in back) state.deepFrom = back.from;
    else if (state.tab !== "dossier" && refreshedAt) state.deepFrom = state.tab === "micro" ? "micro" : "deep";
    setTab("dossier", { replace });
    return true;
  }
  if ((v = arg("sector/", "sector=")) != null) { setCat(v); setTab("matrix", { replace }); return true; }
  if ((v = arg("book/")) != null) {
    if (state.book !== v) { state.book = v; state.bookName = null; }
    setTab(v ? "book" : "books", { replace });
    return true;
  }
  if ((v = arg("report/", "report=")) != null) { state.reportId = Number(v) || null; setTab("reports", { replace }); return true; }
  // old links keep working: the views that moved answer at their new place
  const alias = { today: "home", overview: "home", coins: "matrix", record: "scorecard", sources: "scorecard", x: "social",
    system: "health", deepend: "deep", "deep-end": "deep", memecoins: "deep", tokens: "micro", paper: "books",
    "paper-books": "books", results: "books", majors: "factor", markets: "factor", calls: "books", research: "reports",
    ops: "agents", settings: "setup", keys: "setup", dossier: "dreport", spend: "output", strategies: "lab", "strategy-lab": "lab",
    alpha: "live", "loss-avoidance": "avoid", "factor-edge": "edge" };
  v = alias[h] || h;
  if (v && v !== "coin" && v !== "dossier" && document.getElementById(`tab-${v}`)) {
    if (v === "matrix") setCat("");
    setTab(v, { replace });
    return true;
  }
  return false;
}
addEventListener("popstate", () => {
  if (location.hash === "#about") openAbout();
  else if (!fromHash(location.hash, true, history.state) && !location.hash.startsWith("#micro=")) history.replaceState(null, "", `#${route()}`);
});

// Loaders are guarded (see "load states"): a failing API never blanks another view or stops the rest.
let refreshedAt = 0;
async function refresh() {
  const tab = state.tab;
  refreshedAt = Date.now();
  document.dispatchEvent(new CustomEvent("refresh", { detail: { view: tab } }));   // Today's overview listens
  loadAlerts().catch(console.error);   // the banner shows on every tab; never block the tab on it
  loadHealth().catch(console.error);   // likewise the header's asset count (and the Health tab itself)
  try {
    if (tab === "matrix" || (tab === "coin" && !state.matrix)) await loadMatrix();
    if (tab === "coin") await renderCoin(state.coin);
    if (tab === "sectors") await loadSectors();
    if (tab === "ideas") await loadIdeas();
    if (tab === "reports") await loadReports();
    if (tab === "news") await loadNews();
    if (tab === "social") await loadSocial();
    if (tab === "scorecard") await Promise.all([loadScorecard(), loadIdeaScorecard()]);
  } catch (e) {
    console.error(e);
  }
}

// Each loader, guarded: what a failure message calls it, the panels that show its state (the first one that
// exists gets the explanation; later ones are folds and side panels), and what its data depends on besides its
// arguments.
loadAlerts = guarded("the system status", ["#sys-status"], loadAlerts);
const healthLoad = guarded("the job list", ["#jobs", "#spend", "#counts"], loadHealth);
loadHealth = () => healthLoad().then(renderStatus);      // the status card also says when the job list fails
loadMatrix = guarded("the coin table", ["#matrix", "#matrix-rest"], loadMatrix, () => state.matrixSource);
loadSectors = guarded("the sectors", ["#sectors-map", "#sectors"], loadSectors);
renderCoin = guarded("this coin", ["#coin-body"], renderCoin);     // keyed by its symbol: another coin's page is never "stale"
loadIdeas = guarded("the ideas", ["#ideas", "#ideas-closed"], loadIdeas, () => [state.ideaStatus, state.ideaSource]);
loadScorecard = guarded("the track record", ["#ans-sources-body", "#sc-cards", "#sc-tf", "#sc-bias", "#sc-sector", "#sc-sector-calls"],
  loadScorecard, () => [state.scSource, state.scDays]);
loadIdeaScorecard = guarded("the ideas backtest", ["#ans-ideas-body", "#isc"], loadIdeaScorecard, () => state.iscSplit);
loadReports = guarded("the reports", ["#report-list", "#report"], loadReports, () => state.reportKind);
loadNews = guarded("the headlines", ["#news"], loadNews, () => [state.impact, state.sent, state.newsAsset]);
loadSocial = guarded("the X sentiment", ["#social"], loadSocial, () => state.socialDays);

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  const sec = b.dataset.section, v = lastView[sec] || SECTIONS[sec].views[0];
  if (v === "matrix") setCat("");
  setTab(v);
}));
$("#subnav").addEventListener("click", (e) => {
  const a = e.target.closest("[data-view]");
  if (a) {
    e.preventDefault();
    if (a.dataset.view === "matrix" && !a.dataset.keepcat) setCat("");
    setTab(a.dataset.view);
    return;
  }
  if (e.target.closest("[data-clearcat]")) { setCat(""); setTab("matrix", { replace: true }); renderMatrix(); }
});
$("#matrix-more").addEventListener("click", (e) => {
  state.matrixMore = !state.matrixMore;
  e.currentTarget.setAttribute("aria-expanded", state.matrixMore);
  e.currentTarget.classList.toggle("on", state.matrixMore);
  $("#matrix-more-row").hidden = !state.matrixMore;
  renderMatrix();
});
$("#sectors").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-sector]"); if (tr) openSector(tr.dataset.sector); });
$("#alert-banner").addEventListener("click", (e) => {
  e.preventDefault();
  setTab("health");
  scrollTo({ top: 0 });      // the alerts list leads the Health tab, just under the sticky header
});
$("#brand").addEventListener("click", openAbout);
$("#mast-brand").addEventListener("click", openAbout);     // phones: the rail's logo is gone, the masthead carries it
$("#tier-chips").addEventListener("click", (e) => {
  const t = Number(e.target.dataset.tier);
  if (!t) return;
  state.tiers.has(t) ? state.tiers.delete(t) : state.tiers.add(t);
  e.target.classList.toggle("on", state.tiers.has(t));
  renderMatrix();
});
$("#impact-chips").addEventListener("click", (e) => {
  if (e.target.dataset.impact == null) return;
  state.impact = Number(e.target.dataset.impact);
  document.querySelectorAll("#impact-chips button").forEach((b) => b.classList.toggle("on", b === e.target));
  refilterNews();
});
function chipGroup(sel, attr, apply) {
  $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset[attr] == null) return;
    document.querySelectorAll(`${sel} button`).forEach((x) => x.classList.toggle("on", x === b));
    apply(b.dataset[attr]);
  });
}
chipGroup("#sent-chips", "sent", (v) => { state.sent = v; refilterNews(); });
chipGroup("#social-kind", "kind", (v) => { state.socialKind = v; renderSocial(); });
chipGroup("#social-days", "days", (v) => { state.socialDays = Number(v); loadSocial(); });
chipGroup("#sc-days", "days", (v) => { state.scDays = Number(v); loadScorecard(); });
chipGroup("#src-chips", "src", (v) => { state.matrixSource = v; loadMatrix(); });
chipGroup("#idea-status", "status", (v) => { state.ideaStatus = v; loadIdeas(); });
chipGroup("#isc-policy", "policy", (v) => { state.iscPolicy = v; renderIdeaScorecard(); });
chipGroup("#isc-split", "split", (v) => { state.iscSplit = v; loadIdeaScorecard(); });
$("#idea-source").addEventListener("change", (e) => { state.ideaSource = e.target.value; loadIdeas(); });
// An idea row opens its plan in place; the coin cell spans a coin's rows, so it links to the coin instead.
for (const t of ["#ideas", "#ideas-closed"]) {
  const toggle = (e) => {
    const tr = e.target.closest("tr[data-idea]");
    if (!tr || e.target.closest("a, td.coin")) return;
    if (e.type === "keydown") { if (e.key !== "Enter" && e.key !== " ") return; e.preventDefault(); }
    const id = Number(tr.dataset.idea);
    state.openIdeas.has(id) ? state.openIdeas.delete(id) : state.openIdeas.add(id);
    renderIdeas();
    if (e.type === "keydown") $(`tr[data-idea="${id}"]`)?.focus();
  };
  $(t).addEventListener("click", toggle);
  $(t).addEventListener("keydown", toggle);
}
// A leaderboard row drills into that source's detail below it.
const openSource = (e) => {
  const tr = e.target.closest("tr[data-lbsrc]");
  if (!tr || (e.type === "keydown" && e.key !== "Enter")) return;
  state.scSource = tr.dataset.lbsrc;
  $("#sc-fold").open = true;
  loadScorecard().then(() => $("#sc-fold").scrollIntoView({ behavior: "smooth", block: "start" })).catch(console.error);
};
$("#ans-sources-body").addEventListener("click", openSource);
$("#ans-sources-body").addEventListener("keydown", openSource);
$("#tab-scorecard").addEventListener("click", (e) => {
  const b = e.target.closest("[data-openfold]");
  if (!b) return;
  const f = document.getElementById(b.dataset.openfold);
  f.open = true;
  f.scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#tab-reports").addEventListener("click", (e) => {
  const k = e.target.closest("button[data-rkind]"), b = e.target.closest("[data-report]");
  if (k) { state.reportKind = k.dataset.rkind; loadReports(); }
  if (!b) return;
  e.preventDefault();
  state.reportId = Number(b.dataset.report);
  setTab("reports");                 // pushes #/report/<id>, so Back and links work
});
$("#sc-source").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-src]");
  if (b) { state.scSource = b.dataset.src; loadScorecard(); }
});
let newsTimer;
function refilterNews() { state.newsLimit = NEWS_PAGE; loadNews(); }
$("#news-asset").addEventListener("input", (e) => { state.newsAsset = e.target.value; clearTimeout(newsTimer); newsTimer = setTimeout(refilterNews, 300); });
$("#news-more").addEventListener("click", () => { state.newsLimit = Math.min(NEWS_MAX, state.newsLimit + NEWS_PAGE); loadNews(); });
$("#news").addEventListener("click", (e) => {
  if (!e.target.closest("[data-news-reset]")) return;
  state.impact = 0; state.sent = ""; state.newsAsset = ""; $("#news-asset").value = "";
  ["#impact-chips", "#sent-chips"].forEach((g) => $(g).querySelectorAll("button").forEach((b, i) => b.classList.toggle("on", !i)));
  refilterNews();
});
// An asset chip on any headline (the wire or a coin page) opens the wire filtered to that asset.
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-news-asset]");
  if (!a) return;
  e.preventDefault();
  state.newsAsset = $("#news-asset").value = a.dataset.newsAsset;
  state.newsLimit = NEWS_PAGE;
  state.tab === "news" ? (loadNews(), scrollTo({ top: 0 })) : setTab("news");
});
$("#social").addEventListener("click", (e) => { const r = e.target.closest(".xs-row[data-scope]"); if (r && !e.target.closest("a")) openSocial(r.dataset.scope); });
$("#social").addEventListener("keydown", (e) => {
  const r = e.target.closest(".xs-row[data-scope]");
  if (r && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSocial(r.dataset.scope); }
});
$("#cat-filter").addEventListener("change", (e) => { state.cat = e.target.value; setTab("matrix", { replace: true }); });
$("#search").addEventListener("input", (e) => { state.q = e.target.value; renderMatrix(); });
$("#matrix").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-sym]"); if (tr) openAsset(tr.dataset.sym); });
$("#matrix").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-sym]"); if (tr && e.key === "Enter") openAsset(tr.dataset.sym); });
$("#matrix-rest").addEventListener("click", (e) => { const b = e.target.closest("button[data-sym]"); if (b) openAsset(b.dataset.sym); });
$("#drawer-close").addEventListener("click", () => { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#drawer").classList.contains("open")) $("#drawer-close").click(); });
let drawerOpener = null;
new MutationObserver(() => {
  const open = $("#drawer").classList.contains("open");
  if (open && !drawerOpener) { drawerOpener = document.activeElement || document.body; $("#drawer-close").focus({ preventScroll: true }); }
  else if (!open && drawerOpener) { if (drawerOpener.isConnected) drawerOpener.focus({ preventScroll: true }); drawerOpener = null; }
}).observe($("#drawer"), { attributes: true, attributeFilter: ["class"] });
document.addEventListener("pointerover", (e) => {
  const td = e.target.closest("td[data-tip], td[data-tipid]");
  if (td) td.onpointermove = (ev) => showTip(ev, td.dataset.tip ? sigTip(td.dataset.tip) : tips.get(td.dataset.tipid) || "");
});
document.addEventListener("pointerout", (e) => { if (e.target.closest("td[data-tip], td[data-tipid]")) hideTip(); });
$("#theme").addEventListener("click", () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("albatross.theme", next); } catch {}
});

// Deep links: #/sectors, #/matrix, #/sector/defi, #/coin/BTC, #/ideas, #/scorecard, #/reports, #/report/12,
// #/news, #/social, #/deep, #/deep/<chain>/<address>, #/micro, #/health (older #asset=BTC-style links still work).
// Otherwise reopen the last view.
try {
  const t = localStorage.getItem("albatross.theme");
  if (t) document.documentElement.dataset.theme = t;
} catch {}
const wantAbout = location.hash === "#about";      // read before the router rewrites the hash
if (!fromHash(location.hash, true, history.state) && !location.hash.startsWith("#micro=")) {    // a reload keeps the trail too
  let t = null;
  try { t = localStorage.getItem("albatross.tab"); } catch {}
  setTab(t && t !== "coin" && document.getElementById(`tab-${t}`) ? t : "home", { replace: true });
}
if (wantAbout) openAbout();
setInterval(() => { if (!document.hidden) refresh(); }, 60_000);
// Back to a tab that sat in the background, or back online: refresh straight away.
document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - refreshedAt > 60_000) refresh(); });
addEventListener("online", () => refresh());
