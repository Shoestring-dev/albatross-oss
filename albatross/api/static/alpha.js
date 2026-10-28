"use strict";
// Alpha (review of 2026-10-09, over /api/alpha): Live, Review, Loss avoidance and Factor edge. The suggestions run beside
// what they would replace; nothing here switches anything off. Uses app.js helpers: $, esc, get, ago, guarded, niceTicks.
(() => {
  if (!$("#tab-live")) return;
  const S = { data: null };
  const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
  const num = (v, d = 1) => { if (v == null) return "–"; const a = Math.abs(v).toFixed(d); return `${+a === 0 ? "" : sign(v)}${a}`; };
  const pct = (v, d = 1) => (v == null ? "–" : `${num(v, d)}%`);
  const share = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
  const int = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const usd = (v) => (v == null ? "–" : `${sign(v)}$${Math.round(Math.abs(v)).toLocaleString()}`);
  const tone = (v) => (v == null || Math.abs(v) < 0.05 ? "" : v > 0 ? "up" : "down");
  const UTC = { timeZone: "UTC" };
  const day = (d) => (d ? new Date(String(d).length <= 10 ? `${d}T00:00:00Z` : d).toLocaleDateString("en-US", { month: "short", day: "numeric", ...UTC }) : "–");
  const yday = (d) => new Date(`${String(d).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", ...UTC });
  const when = (d) => `${day(d)}, ${new Date(d).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", ...UTC })} UTC`;
  const chip = ([w, t]) => `<span class="jstate ${t}">${esc(w)}</span>`;
  const PAGE = { avoid: "Loss avoidance", edge: "Factor edge", live: "Live" };
  const go = (view, text) => (view ? ` <a href="#/${view}" class="al-go">${esc(text || PAGE[view] || view)} →</a>` : "");
  const tile = (k, v, d, toneVar, cls = "") => `<div class="kpi${cls ? ` ${cls}` : ""}" style="--tone:${toneVar || "var(--cream)"}"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;

  // ─── Live ───────────────────────────────────────────────────────────────────
  const VCHIP = { pass: ["PASS", "good"], skip: ["SKIP", "critical"], unread: ["NO DOSSIER", "muted"] };
  const WHO = { caller: "Group call", bot: "Bot", fomo: "FOMO trader" };
  const F = { result: null, verdict: "all", kind: "all", shown: 50 };
  const FILTERS = [["result", "Result", [["test", "The test"], ["finished", "Finished"], ["open", "Still open"], ["all", "Both"]]],
    ["verdict", "Verdict", [["all", "Any verdict"], ["pass", "PASS"], ["skip", "SKIP"], ["unread", "NO DOSSIER"]]],
    ["kind", "Who", [["all", "Everyone"], ["caller", "Group calls"], ["bot", "Bot"], ["fomo", "FOMO traders"]]]];
  const FLAG = { dev: "dev wallet", liquidity: "liquidity", mint: "mint authority", tax: "tax", top10: "top-10 holders", organic: "organic volume" };
  const reasons = (why) => {
    const flags = why.filter((k) => FLAG[k]).map((k) => FLAG[k]);
    return [flags.length ? `Red flag${flags.length > 1 ? "s" : ""}: ${flags.join(", ")}` : "", why.includes("young") ? "under 24 hours old" : "",
      why.includes("unpaid") ? "Dex unpaid after 24 h" : ""].filter(Boolean).join(" · ");
  };
  const ageText = (h) => (h == null ? "–" : h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`);
  const price = (p) => (p == null ? "" : p >= 1 ? `$${p.toFixed(2)}` : `$${p.toPrecision(2)}`);
  const pts = (v) => (v == null ? "–" : `${num(v)} points`);

  function renderLive(l, avoidRep, edgeRep) {
    if (!l) { $("#lv-tiles").innerHTML = '<p class="note">The first verdicts arrive within five minutes.</p>'; return; }
    $("#lv-when").textContent = `updated ${ago(l.computed_at)}`;
    const t = l.live.caller, from = when(l.live.from);
    const P = t.pass, K = t.skip;
    if (F.result == null) {               // the hero's own view, once one of its calls has an answer; until then finished calls
      const answered = l.feed.some((r) => r.test && r.kind === "caller" && r.verdict !== "unread" && r.state === "finished");
      Object.assign(F, answered ? { result: "test", kind: "caller" } : { result: "finished" });
    }
    const fin = Math.min(P.finished.n || 0, K.finished.n || 0) >= 20;          // enough finished calls on each side to read
    const use = fin ? "finished" : "so_far";
    const gap = P[use].n && K[use].n ? P[use].mean - K[use].mean : null;
    const finishedN = (P.finished.n || 0) + (K.finished.n || 0) + (t.unread.finished.n || 0);
    const due = t.next_due && new Date(t.next_due) < new Date() ? `finishing now, the last at ${when(t.last_due)}` : t.next_due ? `due from ${when(t.next_due)} to ${when(t.last_due)}` : "";
    $("#lv-lead").innerHTML = fin
      ? `${gap > 0 ? "So far, yes." : "Not so far."} Of the calls since ${from} that have finished, PASS calls averaged <b class="${tone(P.finished.mean)}">${pct(P.finished.mean)}</b>
        and SKIP calls <b class="${tone(K.finished.mean)}">${pct(K.finished.mean)}</b> after 24 hours. ${int(t.open_calls)} more are still running.`
      : `Too early to say. The test started when the history the checklist was built on ended, ${from}. ${finishedN ? `${int(finishedN)} of its calls have finished so far` : `None of its ${int(t.open_calls)} calls has finished its 24 hours yet`}.
        ${t.next_due && new Date(t.next_due) < new Date() ? "The first results are due now" : `The first results are due ${when(t.next_due)}`}${t.last_due ? ` and the last at ${when(t.last_due)}` : ""}.`;
    const hist = (l.windows || []).find((w) => w.window === "all" && w.kind === "caller");
    const perCoin = avoidRep?.checklists?.find((c) => c.key === "rebuilt_clean")?.held;
    const fx = l.factor || {}, ic = edgeRep?.ic;
    const factorTiles = fx.held ? tile(`This week's lists · ${day(fx.held_day)} to ${day(fx.closes)}`,
      `<span class="${tone(fx.held.total_pct)}">${pct(fx.held.total_pct)}</span>`,
      `own list ${pct(fx.held.own.for_position_pct)} · short list ${pct(fx.held.short.for_position_pct)} for the short · ${fx.days_in} of 7 days, before costs`,
      "var(--good)") + (ic?.weeks ? tile(`${ic.weeks} weeks of history`, `${pct(ic.spread_mean)} <small>a week</small>`,
      `right order in ${ic.right_order} of ${ic.weeks} weeks · the top fifth beat the bottom in ${ic.top_beat_bottom}`, "var(--muted)") : "") : "";
    $("#lv-tiles").innerHTML = `
      <h3>Memecoin calls <span class="cat">group calls; a $100 buy at each call's price, after every cost, held 24 hours</span></h3>
      <div class="kpis al-kpis">
        ${tile(`The test · calls since ${day(l.live.from)}, 22:00 UTC`, gap == null ? "–" : `${num(Math.abs(gap))} <small>points ${gap >= 0 ? "better" : "worse"} for PASS</small>`,
          `<span class="al-dl"><span class="al-dk">${fin ? "after 24 hours" : "average so far"}</span>PASS ${pct(P[use].mean)} · SKIP ${pct(K[use].mean)} a call · ${int(P[use].n)} and ${int(K[use].n)} calls</span>
           <span class="al-dl"><span class="al-dk">lost 90% or more</span>PASS ${share(P[use].lost90)} · SKIP ${share(K[use].lost90)}</span>
           <a href="#/live" class="al-go al-totest" data-totest>See these calls →</a>`,
          gap > 0 ? "var(--good)" : "var(--cream)", "al-hero")}
        ${tile("Finished so far", `${int(finishedN)} <small>calls</small>`, fin ? `median PASS ${pct(P.finished.median)} · SKIP ${pct(K.finished.median)}`
          : `a read needs 20 finished on each side · ${int(P.finished.n || 0)} PASS, ${int(K.finished.n || 0)} SKIP`, "var(--muted)")}
        ${tile("Still running", `${int(t.open_calls)} <small>calls</small>`, t.last_due ? `the last finishes ${when(t.last_due)}` : "none", "var(--axis)")}
        ${hist ? tile("The history it was built on", `PASS ${pct(hist.pass.mean)} · SKIP ${pct(hist.skip.mean)}`,
          `a call, medians ${pct(hist.pass.median)} and ${pct(hist.skip.median)} · Sep 30 to Oct 8, every call counted`, "var(--muted)", "al-muted") : ""}
      </div>
      <p class="al-bridge">${perCoin ? `Counting each coin once, as the paper books do, the calls the checklist marks PASS averaged ${pct(perCoin.mean)} on that history. ` : ""}
        Bot buys since ${day(l.live.from)}: PASS ${pct(l.live.bot.pass.so_far.mean)} (${int(l.live.bot.pass.so_far.n || 0)} buys) · SKIP ${pct(l.live.bot.skip.so_far.mean)} (${int(l.live.bot.skip.so_far.n || 0)} buys) so far, paper only.${go("avoid")}</p>
`;
    renderWindows(l.windows || []);
    renderLists(fx, factorTiles);
    drawFeed();
  }

  function renderWindows(ws) {
    const cell = (s) => (s && s.n ? `<b class="${tone(s.mean)}">${pct(s.mean)}</b><span class="sub"> median ${pct(s.median)} · ${share(s.won)} made money · ${int(s.n)}</span>` : "–");
    const table = (kind, title) => `<h4 class="al-lane">${title}</h4><div class="table-wrap"><table class="plain al-table"><thead><tr><th>Calls</th>
      <th>Marked PASS</th><th>Marked SKIP</th><th>Every call</th></tr></thead><tbody>${ws.filter((w) => w.kind === kind).map((w) => `<tr>
      <td class="al-name">${esc(w.label)}</td><td class="al-vs" data-k="PASS">${cell(w.pass)}</td><td class="al-vs" data-k="SKIP">${cell(w.skip)}</td>
      <td class="al-vs" data-k="Every call">${cell(w.every)}</td></tr>`).join("")}</tbody></table></div>`;
    $("#lv-windows").innerHTML = `<details class="fold sub-fold"><summary>Every window <span class="cat">open, last 7 days and since Sep 30, each call counted at its own price</span></summary>
      ${table("caller", "Group calls")}${table("bot", "Bot buys, paper only")}</details>`;
  }

  function renderLists(fx, tiles) {
    if (!fx?.held) { $("#lv-factor").innerHTML = ""; return; }
    const list = (side) => `<div class="al-side"><h4>${side === "own" ? "Own · top fifth" : "Short · bottom fifth"}
      <span class="${tone(fx.held[side].for_position_pct)}">${pct(fx.held[side].for_position_pct)}</span></h4>
      <ol>${fx.held[side].coins.map((c) => `<li><span class="al-r">${c.rank}</span><a href="#/coin/${encodeURIComponent(c.symbol)}">${esc(c.symbol)}</a>
      <span class="${tone(c.for_position_pct)}" title="price ${pct(c.move_pct)} since the pick">${pct(c.for_position_pct)}</span></li>`).join("")}</ol></div>`;
    const ch = fx.changes, part = (s) => [ch[s].add.length ? `add ${ch[s].add.join(", ")}` : "", ch[s].drop.length ? `drop ${ch[s].drop.join(", ")}` : ""].filter(Boolean).join(" and ");
    const [own, short] = [part("own"), part("short")];
    const today = own || short ? `The ranking from the ${day(fx.ranked_day)} close would ${own ? `${own} on the own list` : ""}${own && short ? ", and " : ""}${short ? `${short} on the short list` : ""}.`
      : `The ranking from the ${day(fx.ranked_day)} close picks the same coins.`;
    $("#lv-factor").innerHTML = `<h3>Factor rank <span class="cat">own the top fifth, short the bottom fifth</span></h3><div class="kpis al-kpis al-kpis2">${tiles}</div>
      <h4 class="al-lane">The lists held this week <span class="cat">picked on the ${day(fx.held_day)} close, held to ${day(fx.closes)}; each coin's move for the position · prices ${ago(fx.marked_at)}</span></h4>
      <div class="panel al-lists">${list("own")}${list("short")}</div>
      <p class="al-bridge">On the short list a falling coin is a gain, so it shows green. ${today} One week proves little.${go("edge", "Every week since Aug 2025")}</p>`;
  }

  const inResult = (r, v) => v === "all" || (v === "test" ? r.test : v === "finished" ? r.state === "finished" : r.state !== "finished");
  const matches = (r, skip) => (skip === "result" || inResult(r, F.result))
    && (skip === "verdict" || F.verdict === "all" || r.verdict === F.verdict) && (skip === "kind" || F.kind === "all" || r.kind === F.kind);
  function drawFeed() {
    const l = S.data?.live;
    if (!l) return;
    const calls = (rows) => rows.reduce((a, r) => a + r.calls, 0);          // counts are calls, as the tiles count them
    const count = (k, v) => calls(l.feed.filter((r) => matches(r, k) && (k === "result" ? inResult(r, v) : v === "all" || r[k] === v)));
    $("#lv-filters").innerHTML = FILTERS.map(([k, label, opts]) => `<div class="chips" role="group" aria-label="${label}"><span class="al-flabel">${label}</span>${opts.map(([v, w]) =>
      `<button type="button" class="${F[k] === v ? "on" : ""}" aria-pressed="${F[k] === v}" data-k="${k}" data-v="${v}">${esc(w)}<span class="n">${int(count(k, v))}</span></button>`).join("")}</div>`).join("");
    const rows = l.feed.filter((r) => matches(r));
    // every call in a row at its own result (a merged row holds several), so these sums match the tiles
    const each = rows.flatMap((r) => r.each.map(([net, done]) => ({ net, done, verdict: r.verdict })));
    const priced = each.filter((c) => c.net != null), waiting = each.length - priced.length;
    const done = priced.filter((c) => c.done), live = priced.filter((c) => !c.done);
    const avg = (xs) => (xs.length ? xs.reduce((a, c) => a + c.net, 0) / xs.length : null);
    const med = (xs) => { if (!xs.length) return null; const v = xs.map((c) => c.net).sort((a, b) => a - b), m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };
    const part = (xs, word, after) => (xs.length ? `${word ? `${word} ` : ""}${int(xs.length)}: average <b class="${tone(avg(xs))}">${pct(avg(xs))}</b>, median ${pct(med(xs))}${after}` : "");
    const split = (xs, after) => (F.verdict !== "all" ? part(xs, "", after) : ["pass", "skip", "unread"].map((v) => part(xs.filter((c) => c.verdict === v), VCHIP[v][0], after)).filter(Boolean).join(" · "));
    const kword = { all: " from everyone (group calls, the bot and FOMO traders)", caller: " from group calls", bot: " by the bot", fomo: " by FOMO traders" }[F.kind];
    const vword = { all: "", pass: "PASS ", skip: "SKIP ", unread: "no-dossier " }[F.verdict];
    const lines = F.result === "test"
      ? [priced.length ? `<span class="al-sumk">so far</span> ${split(priced, "")}` : "", done.length ? `<span class="al-sumk">finished</span> ${split(done, "")}` : ""]
      : [done.length ? `<span class="al-sumk">finished</span> ${split(done, "")}` : "", live.length ? `<span class="al-sumk">still running</span> ${split(live, " so far")}` : ""];
    $("#lv-sum").innerHTML = [`${int(each.length)} ${vword}calls${kword}${F.result === "test" ? ", in the test" : ""}`, ...lines,
      waiting ? `${int(waiting)} waiting for a price` : ""].filter(Boolean).join("<br>");
    const shownPct = (v) => (v < -100 ? "−100%" : pct(v));
    const result = (r) => (r.net == null ? '<span class="sub">waiting for a price</span>'
      : r.state === "finished" ? `${r.hit_2x ? '<span class="al-2x" title="It reached twice the call price at some point in its 24 hours">2x</span>' : ""}<b class="${tone(r.net)}"${r.net < -100 ? ` title="${pct(r.net)} after costs"` : ""}>${shownPct(r.net)}</b>`
        : `<span class="al-open ${tone(r.net)}">${shownPct(r.net)}</span> <span class="sub">${r.state === "settling" ? "settling" : "so far"}</span>`);
    const who = (r) => (r.kind === "bot" ? "Bot" : r.who.length ? `${esc(r.who[0])}${r.who.length > 1 ? ` <span class="sub">+${r.who.length - 1}</span>` : ""}` : WHO[r.kind]);
    const coin = (r) => {
      const [chain, address] = String(r.scope).split(":");
      const label = r.symbol || `${address.slice(0, 4)}…${address.slice(-4)}`;
      return `<a href="#/deep/${encodeURIComponent(chain)}/${encodeURIComponent(address)}" title="Open the dossier">${esc(label)}</a>${r.calls > 1 ? ` <span class="sub">· ${r.calls} calls</span>` : ""}${r.price ? ` <span class="sub">at ${price(r.price)}</span>` : ""}`;
    };
    const shown = rows.slice(0, F.shown);
    $("#lv-feed").innerHTML = shown.length ? `<div class="table-wrap"><table class="plain al-table al-feed"><thead><tr><th>When</th><th>Coin</th><th>Verdict</th>
      <th class="num">Result</th><th>Why</th><th>Who</th><th class="num">Coin age</th></tr></thead><tbody>${shown.map((r) => `<tr${r.test ? ' class="al-test"' : ""}>
      <td class="al-when">${esc(ago(r.at))}${r.test && F.result !== "test" ? '<span class="al-tag" title="Called after the history the checklist was built on: part of the test">test</span>' : ""}</td>
      <td class="al-coin">${coin(r)}</td><td class="al-verdict">${chip(VCHIP[r.verdict])}</td>
      <td class="num al-result">${result(r)}</td><td class="al-why">${esc(reasons(r.why))}</td><td class="al-who">${who(r)}</td>
      <td class="num al-age">${ageText(r.age_h)}</td><td class="al-meta">${esc(ago(r.at))}${r.test && F.result !== "test" ? " · test" : ""} · ${who(r)} · coin age ${ageText(r.age_h)}</td></tr>`).join("")}</tbody></table></div>
      ${rows.length > F.shown ? `<p class="al-more"><button type="button" class="more-toggle" data-more>Show ${Math.min(50, rows.length - F.shown)} more rows of ${int(rows.length - F.shown)}</button></p>` : ""}`
      : '<p class="note">No calls match.</p>';
  }
  $("#tab-live").addEventListener("click", (e) => {
    if (e.target.closest("[data-totest]")) {         // the hero tile: the same calls, row by row
      e.preventDefault();
      Object.assign(F, { result: "test", verdict: "all", kind: "caller", shown: 50 });
      drawFeed();
      $("#lv-filters")?.scrollIntoView({ behavior: "smooth", block: "start" });
      $("#lv-filters button.on")?.focus({ preventScroll: true });
      return;
    }
    const b = e.target.closest("#lv-filters button[data-k]");
    if (b) { F[b.dataset.k] = b.dataset.v; F.shown = 50; drawFeed(); return; }
    if (e.target.closest("[data-more]")) {         // keyboard focus moves to the first new row, not back to the top
      F.shown += 50; drawFeed();
      $(`#lv-feed tbody tr:nth-child(${F.shown - 49}) .al-coin a`)?.focus();
    }
  });

  // ─── Review ─────────────────────────────────────────────────────────────────
  const VERDICT = { keep: ["Keep", "good"], adjust: ["Adjust", "warning"], abandon: ["Abandon", "critical"], fixed: ["Fixed", "good"] };
  const ORDER = { abandon: 0, adjust: 1, keep: 2, fixed: 3 };
  const STATUS = { running: "running as before", "on trial": "on trial", proposed: "proposed, not built", done: "done", "paper only": "paper only" };
  const LANE = { memecoins: "Memecoins", majors: "Majors", measuring: "Measuring" };
  function renderReview(r) {
    $("#al-reviewed").textContent = `the reference install, reviewed ${day(r.reviewed)}`;
    $("#al-summary").textContent = r.summary;
    $("#al-decisions").innerHTML = `<div class="panel al-decide">${r.decisions.map((d) => `<div class="al-q"><div><b>${esc(d.q)}</b><p>${esc(d.why)}</p></div>
      ${chip([d.status || "Open", d.status ? "muted" : "warning"])}</div>`).join("")}</div>`;
    const bet = (b) => `<article class="panel al-bet${b.rank <= 2 ? " lead" : ""}">
      <header><span class="al-rank">${b.rank}</span><h4>${esc(b.name)}</h4><span class="cat">${esc(b.kind)}</span></header>
      <p>${esc(b.evidence)}</p><p class="sub">${esc(b.test)}${go(b.page)}</p></article>`;
    $("#al-bets").innerHTML = `<h4 class="al-lane">Working</h4><div class="al-bets al-bets2">${r.bets.filter((b) => b.rank <= 2).map(bet).join("")}</div>
      <h4 class="al-lane">Worth testing</h4><div class="al-bets al-bets3">${r.bets.filter((b) => b.rank > 2).map(bet).join("")}</div>`;
    $("#al-weak").textContent = `Weak or not worth chasing: ${r.weak}`;
    const lanes = [...new Set(r.components.map((c) => c.lane))];
    const phone = matchMedia("(max-width: 700px)").matches;           // lanes start folded on a phone
    $("#al-components").innerHTML = lanes.map((lane, i) => {
      const cs = r.components.filter((c) => c.lane === lane).sort((a, b) => ORDER[a.verdict] - ORDER[b.verdict]);
      const n = (v) => cs.filter((c) => c.verdict === v).length;
      const counts = ["abandon", "adjust", "keep", "fixed"].filter(n).map((v) => `${n(v)} ${VERDICT[v][0].toLowerCase()}`).join(" · ");
      return `<details class="fold al-lanefold"${phone ? "" : " open"}><summary>${esc(LANE[lane] || lane)} <span class="cat">${counts}</span></summary>
        <div class="table-wrap"><table class="plain al-table al-review"><colgroup><col style="width:19%"><col style="width:10%">
        <col style="width:31%"><col style="width:26%"><col style="width:14%"></colgroup>${i === 0 || phone ? `<thead><tr><th>Component</th><th>Verdict</th><th>What the data shows</th>
        <th>Suggested change</th><th>Where it stands</th></tr></thead>` : ""}<tbody>${cs.map((c) => `<tr>
        <td class="al-name">${esc(c.name)}</td><td class="al-verdict">${chip(VERDICT[c.verdict] || [c.verdict, "muted"])}</td>
        <td class="al-text">${esc(c.finding)}</td><td class="al-text al-change">${esc(c.change || "")}</td>
        <td class="al-status">${esc(STATUS[c.status] || c.status)}${c.page ? go(c.page) : ""}</td></tr>`).join("")}</tbody></table></div></details>`;
    }).join("");
  }

  // ─── Loss avoidance ─────────────────────────────────────────────────────────
  const LINE = { age: "Coin age", dev: "Dev wallet", liquidity: "Liquidity", organic: "Organic volume", top10: "Top-10 holders",
    mint: "Mint authority", tax: "Tax", lp: "LP lock", insiders: "Insiders", wallets: "Wallet clusters", dexpaid: "Dex listing paid",
    flow: "Buy pressure", holders: "Holder growth", calls: "Calls so far", callers: "Callers", x: "X posts", fit: "Meme fit",
    smart: "Smart wallets", fomo: "FOMO traders", reddit: "Reddit", github: "GitHub" };
  const vs = (s) => {
    const p = s?.vs_every_call;
    if (!p || !p.days) return "–";
    return `<span class="${tone(p.mean)}">${pts(p.mean)}</span><span class="sub"> ahead on ${Math.round(p.share_up * p.days)} of ${p.days} days</span>`;
  };
  const stat = (s, key, f) => (s && s.n ? f(s[key]) : "–");
  function bookTable(rows, first, label, mark = () => "") {
    return `<div class="table-wrap"><table class="plain al-table al-num"><thead><tr><th>${esc(first)}</th><th class="num">Trades</th>
      <th class="num">Average</th><th class="num">Median</th><th class="num">Lost 90%+</th><th class="num">Sold at 2x</th>
      <th>Against every call, day by day</th></tr></thead><tbody>${rows.map((r) => `<tr class="${mark(r)}"><td class="al-name">${esc(label(r))}</td>
      <td class="num" data-k="trades">${int(r.held.n)}</td><td class="num" data-k="average">${stat(r.held, "mean", pct)}</td>
      <td class="num" data-k="median">${stat(r.held, "median", pct)}</td><td class="num" data-k="lost 90%+">${stat(r.held, "lost90", share)}</td>
      <td class="num" data-k="sold at 2x">${stat(r.sold_2x, "mean", pct)}</td><td class="al-vs">${r.key === "every" ? '<span class="sub">the baseline</span>' : vs(r.held)}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  function renderAvoid(a) {
    if (!a) { $("#av-body").innerHTML = '<p class="note">The first numbers arrive with the next hourly run.</p>'; return; }
    $("#av-when").textContent = `updated ${ago(a.computed_at)}`;
    const every = a.checklists.find((c) => c.key === "every")?.held, clean = a.checklists.find((c) => c.key === "rebuilt_clean")?.held;
    const lost = (s) => (s?.lost90 == null ? "–" : share(s.lost90));
    $("#av-lead").innerHTML = `Counting each coin once, as the paper books do, every group call averaged <b>${pct(every?.mean)}</b> a trade from ${day(a.since)} to ${day(a.settled_to)}
      (${int(every?.n)} trades), and ${lost(every)} of them lost 90% or more. Calls the rebuilt checklist marks PASS averaged
      <b>${pct(clean?.mean)}</b>, and ${lost(clean)} lost 90% or more. Avoiding losses is the edge here: nothing on this page makes money yet.${go("live", "Watch it mark each call live")}`;
    const rules = a.rules.map((r) => {
      const f = r.forward, h = r.history, need = r.read_at.trades, got = f.n || 0;
      return `<article class="panel al-rule"><h4>${esc(r.name)}</h4>
        <p class="bk-due">${got >= need ? chip(["Ready to read", "good"]) : chip(["Too early", "muted"])}<b>first read ${day(r.read_at.date)} or ${int(need)} trades</b>
          <span class="vbar" title="${int(got)} of ${int(need)} trades"><i style="width:${Math.min(100, got / need * 100).toFixed(0)}%"></i></span><span class="sub">${int(got)} of ${int(need)}</span></p>
        <p class="al-big">${got ? `<b class="${tone(f.mean)}">${pct(f.mean)}</b> a trade over ${int(got)} trades on ${f.days} days; against every call ${vs(f)}` : `No finished trades yet: recording since ${day(r.since)}.`}</p>
        <p class="sub">On the history it was designed on: ${pct(h.mean)} a trade over ${int(h.n)} trades.</p></article>`;
    }).join("");
    const b = a.bot;
    const lines = a.lines.map((l) => `<tr><td class="al-name">${esc(LINE[l.key] || l.key)}</td><td data-k="red flag today">${l.red_flag_today ? "yes" : "–"}</td>
      <td data-k="rebuilt">${l.red_flag_rebuilt ? "yes" : "–"}</td>${["good", "warn", "bad"].map((st) => `<td class="num" data-k="${st}">${l[st].n ? `${pct(l[st].mean)} <span class="sub">(${int(l[st].n)})</span>` : "–"}</td>`).join("")}</tr>`).join("");
    $("#av-body").innerHTML = `
      <h3>Fixed in advance <span class="cat">recorded from Oct 10, before any of these trades existed</span></h3>
      <div class="al-rules">${rules}</div>
      <p class="al-bridge">The second rule is the PASS side of the test on Live, which counts every call and starts from Oct 8, 22:00 UTC, when the history ends.
        These cards count each coin once and start on Oct 10, the day after the rules were written.${go("live")}</p>
      <h3>Coin age at the call <span class="cat">the strongest signal</span></h3>
      ${bookTable(a.ages, "Age", (r) => r.band)}
      <h3>Today's checklist and the rebuilt one</h3>
      ${bookTable(a.checklists, "Calls", (r) => r.name, (r) => (r.key === "rebuilt_clean" ? "al-key" : r.key === "every" ? "al-base" : ""))}
      <p class="al-bridge">The rebuilt checklist keeps today's dev wallet, liquidity, mint, tax, top-10 and organic red flags, adds two (younger than 24 hours, and a Dex
        listing still unpaid after 24 hours) and shows LP lock, insiders and wallets as information. It was designed on this history, so these numbers flatter it.</p>
      <details class="fold sub-fold"><summary>Each checklist line <span class="cat">average 24-hour result by the line's status, calls in brackets</span></summary>
        <div class="table-wrap al-narrow"><table class="plain al-table al-num"><thead><tr><th>Line</th><th>Red flag today</th><th>Rebuilt</th>
          <th class="num">Good</th><th class="num">Warn</th><th class="num">Bad</th></tr></thead><tbody>${lines}</tbody></table></div>
        <p class="al-bridge">A line works when "bad" does clearly worse than "good". Several point the other way: their "good" marks coins that had already run before the call.</p></details>
      <h3>Bot buys by age <span class="cat">paper only: nothing is sent to the bot</span></h3>
      <div class="table-wrap al-narrow"><table class="plain al-table al-num"><thead><tr><th>Age at the buy</th><th class="num">Buys</th>
        <th class="num">Average</th><th class="num">Median</th></tr></thead><tbody>${b.bands.map((r) => `<tr><td class="al-name">${esc(r.band)}</td>
        <td class="num" data-k="buys">${int(r.held.n)}</td><td class="num" data-k="average">${stat(r.held, "mean", pct)}</td>
        <td class="num" data-k="median">${stat(r.held, "median", pct)}</td></tr>`).join("")}</tbody></table></div>
      <p class="al-bridge">All ${int(b.all.n)} of its buys we priced: ${pct(b.all.mean)} a trade, ${usd(b.all.usd)} at $100 each, held 24 hours.
        Only the coins 24 hours or older: ${int(b.gated.n)} buys, ${pct(b.gated.mean)} a trade, ${usd(b.gated.usd)}.
        Buys whose coin age we don't know are left out of the second figure. ${esc(b.note)}</p>`;
  }

  // ─── Factor edge ────────────────────────────────────────────────────────────
  function icChart(rows, width) {
    if (rows.length < 2) return '<p class="note">Not enough weeks yet.</p>';
    const W = Math.max(280, Math.round(width || 720)), H = 170, L = 34, R = 8, T = 10, B = 22;
    const vals = rows.map((r) => r.ic), lo = Math.min(-0.2, ...vals), hi = Math.max(0.4, ...vals);
    const y = (v) => T + (hi - v) / (hi - lo) * (H - T - B), bw = (W - L - R) / rows.length;
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const ticks = niceTicks(lo, hi, 4);
    const bars = rows.map((r, i) => { const y0 = y(0), y1 = y(r.ic);
      return `<rect class="${r.ic >= 0 ? "up" : "down"}${r.live ? " live" : ""}" x="${(L + i * bw + 1).toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${Math.max(1, Math.abs(y1 - y0)).toFixed(1)}"><title>Week of ${day(r.day)}: ${r.ic >= 0 ? "right" : "wrong"} order (rank correlation ${num(r.ic, 2)}); top fifth minus bottom fifth ${pct(r.spread)}${r.live ? " · live" : ""}</title></rect>`; }).join("");
    return `<svg class="al-ic" viewBox="0 0 ${W} ${H}" role="img" aria-label="Each week's rank correlation, ${rows.length} weeks">
      ${ticks.map((t) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/><text class="lbl" x="${L - 6}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${num(t, 1)}</text>`).join("")}
      ${bars}<line class="mean" x1="${L}" x2="${W - R}" y1="${y(mean).toFixed(1)}" y2="${y(mean).toFixed(1)}"/>
      <text class="lbl" x="${L}" y="${H - 6}">${yday(rows[0].day)}</text><text class="lbl" x="${W - R}" y="${H - 6}" text-anchor="end">${yday(rows[rows.length - 1].day)}</text></svg>`;
  }
  const SIDE = { long_short: "Own the top fifth, short the bottom", long_only: "Own the top fifth, against every coin", long_hedged: "Own the top fifth, hedged with BTC and ETH" };
  const STATE = { queued: "queued", rejected: "didn't pass", survivor: "passed; next in line for paper trading", paper: "paper trading", champion: "champion", retired: "retired" };
  function renderEdge(e) {
    if (!e) { $("#ed-body").innerHTML = '<p class="note">The first numbers arrive with the next hourly run.</p>'; return; }
    $("#ed-when").textContent = `updated ${ago(e.computed_at)}`;
    const ic = e.ic;
    $("#ed-lead").innerHTML = ic.weeks ? `Over ${ic.weeks} weeks, from ${yday(ic.first)} to ${yday(ic.last)}, the ranking put the coins in the right order in <b>${ic.right_order}</b>
      weeks, and the top fifth beat the bottom fifth in ${ic.top_beat_bottom}, by <b>${pct(ic.spread_mean)}</b> a week on average before costs.
      The ordering holds up better than the size of the gap. ${ic.live_weeks ? `${ic.live_weeks} of these week${ic.live_weeks === 1 ? " was" : "s were"}
      ranked live, the rest back-tested.` : "All of these weeks are back-tested: a live week is graded seven days after the ranking it starts from."}${go("factor", "Factor rank")}` : "No weeks yet.";
    const lw = e.lab_windows || {}, span = (w) => (w ? `${new Date(`${String(w[0]).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", year: "numeric", ...UTC })} to ${new Date(`${String(w[1]).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", year: "numeric", ...UTC })}` : "");
    const lab = (t) => (t?.discovery ? `<span title="t ${num(t.discovery.t, 2)}">${pct(t.discovery.mean)}</span>` : "–");
    const side = (s) => `<tr><td class="al-name">${esc(SIDE[s.side])}</td><td class="num" data-k="per week, older">${lab(s.today)}</td>
      <td class="num" data-k="per week, last 26">${s.today?.holdout ? pct(s.today.holdout.mean) : "–"}</td><td class="al-status" data-k="">${esc(s.today ? STATE[s.today.status] || s.today.status : "not tested yet")}</td></tr>`;
    const best = e.sides.filter((s) => s.best).map((s) => `<li><b>${esc(SIDE[s.side])}:</b> ${esc(s.best.name)} <span class="sub">(${esc(STATE[s.best.status] || s.best.status)})</span></li>`).join("");
    const listChip = (c) => `<span class="al-list-chip ${c.side === "top" ? "own" : "short"}">${c.joining ? "would join " : ""}${c.side === "top" ? "own" : "short"}</span>`;
    const ex = e.exceptions.length ? `<div class="table-wrap al-narrow"><table class="plain al-table al-events"><thead><tr><th>Date</th><th>Coin</th><th>List</th><th>Event</th></tr></thead><tbody>
      ${e.exceptions.flatMap((x) => x.coins.map((c) => `<tr><td class="al-when al-date">${day(x.at)}</td><td class="al-ev-coin"><a class="al-go" href="#/coin/${encodeURIComponent(c.symbol)}">${esc(c.symbol)}</a></td>
      <td class="al-ev-list">${listChip(c)}</td><td class="al-text">${esc(x.title)}</td></tr>`)).join("")}</tbody></table></div>`
      : '<div class="panel"><p class="al-bridge">Nothing on the calendar for this week\'s lists.</p></div>';
    $("#ed-body").innerHTML = `
      <h3>Each week: did the order hold? <span class="cat">a bar above the line means the ranking ordered the next 7 days' returns the right way</span></h3>
      <div class="panel al-chart"><div id="ed-chart"></div><p class="al-legend"><span><span class="al-sw up"></span>right order</span> <span><span class="al-sw down"></span>wrong order</span>
        <span><span class="al-sw mean"></span>the average week</span> <span>live weeks will be outlined</span></p></div>
      <div class="table-wrap al-narrow al-gap"><table class="plain al-table al-num"><thead><tr><th>Quarter</th><th class="num">Weeks in the right order</th></tr></thead>
        <tbody>${e.quarters.map((q) => `<tr><td class="al-name">${esc(q.quarter)}</td><td class="num" data-k="right order">${q.right_order ?? "–"} of ${q.weeks}</td></tr>`).join("")}</tbody></table></div>
      <h3>Events on this week's coins <span class="cat">the lists held since ${day(e.held_day)}, and coins the ${day(e.ranked_on)} ranking would add</span></h3>
      ${ex}
      <h3>Three ways to trade it <span class="cat">today's ranking tested in the strategy lab, after costs, funding left out</span></h3>
      <div class="table-wrap"><table class="plain al-table al-num"><thead><tr><th>Version</th><th class="num">Per week, ${span(lw.older) || "older data"}</th>
        <th class="num">Per week, ${span(lw.recent) || "last 26 weeks"}</th><th>Lab verdict</th></tr></thead><tbody>${e.sides.map(side).join("")}</tbody></table></div>
      <p class="al-bridge">The lab tests on daily prices back to ${span(lw.older)?.split(" to ")[0] || "early 2024"}, longer than the chart above, which starts when the ranking's own record does.
        Shorting the bottom fifth means shorting small coins only Hyperliquid lists. The hedged version needs only BTC and ETH shorts.
        Which of them you can trade depends on where you are.${go("lab", "Strategy lab")}</p>
      ${best ? `<details class="fold sub-fold"><summary>The best each version has found in the lab</summary><ul class="al-list">${best}</ul></details>` : ""}`;
    drawChart();
  }
  // drawn at the panel's own width, so its labels stay the page's size on any screen
  function drawChart() {
    const host = $("#ed-chart");
    if (host && S.data?.edge) host.innerHTML = icChart(S.data.edge.weekly, host.clientWidth);
  }
  let resized;
  addEventListener("resize", () => { clearTimeout(resized); resized = setTimeout(() => { if (state.tab === "edge") drawChart(); }, 200); });

  async function load() {
    S.data = await get("/api/alpha");
    renderLive(S.data.live, S.data.avoid, S.data.edge);
    renderReview(S.data.review);
    renderAvoid(S.data.avoid);
    renderEdge(S.data.edge);
  }
  const loadGuarded = guarded("the Alpha pages", ["#lv-tiles", "#al-components", "#av-body", "#ed-body"], load);
  // the chart is drawn again once its page shows: drawn while hidden, it had no width to fit
  const onView = (v) => { if (["live", "review", "avoid", "edge"].includes(v)) loadGuarded().then(() => { if (v === "edge") drawChart(); }).catch(console.error); };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => onView(e.detail.view));
  if (["#tab-live", "#tab-review", "#tab-avoid", "#tab-edge"].some((t) => $(t)?.classList.contains("on"))) loadGuarded().catch(console.error);
})();
