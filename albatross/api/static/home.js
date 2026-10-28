"use strict";
// Home (the redesign, 2026-10-06): is it working, and what should I do? The answer comes from the paper books by a
// fixed template and goes in the masthead (gallery.js, "homeanswer"); then every book's standing, the CEO memo's
// actions with buttons that ask first (albatross/api/memo.py), the dates that matter, factor rank's shortlist and one
// status line. Uses app.js helpers: $, esc, get, guarded, ago, setTab, state, openAsset.
(() => {
  const tab = $("#tab-home");
  if (!tab) return;
  const H = { books: null, memo: null, intel: null, health: null, plan: null, confirm: null, busy: false, error: null };
  const M = "−";
  const sgn = (v, d = 1) => `${v > 0 ? "+" : v < 0 ? M : ""}${Math.abs(v).toFixed(d)}`;
  const usd = (v) => `${v > 0 ? "+" : v < 0 ? M : ""}$${Math.round(Math.abs(v)).toLocaleString()}`;
  const day = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "–");
  const WORD = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];
  const word = (n) => WORD[n] ?? String(n);
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  // ─── the books: comparison, verdict, the answer ─────────────────────────────
  const CMP = {
    "holding the same calls 24 hours": "holding the same calls for 24 hours",
    "every Telegram call since this book began": "every Telegram call",
    "every call that had a scored dossier since this book began": "every scored call",
    "every call that had a scored dossier, sold at 2x, since this book began": "scored calls sold at 2x",
    "the same side on an average coin the model called that hour, before costs": "the same side on an average coin, before costs",
  };
  const cmp = (s) => CMP[s] || s;
  const PHRASE = { take_profit: "selling at 2x", fomo: "buying what FOMO-ranked traders buy", clean: "skipping calls with a red flag",
    score: "taking only calls scored 85 or more", score_tp: "taking calls scored 85 or more and selling at 2x", bot: "copying the bot's entries" };
  // The fade book's entries sit at the candle close, about 8 minutes before its calls exist: its verdict is withheld.
  const AUDIT = new Set(["fade_quant"]);
  const state_ = (b) => (AUDIT.has(b.key) ? "audit" : b.verdict?.status || "too_early");
  const perTrade = (b) => (b.unit === "equity" || !b.per_trade ? null : b.per_trade.value);
  const ptText = (b) => { const v = perTrade(b); return v == null ? "–" : b.unit === "R" ? `${sgn(v, 2)} R` : `${sgn(v)}%`; };

  function comparison(b) {
    const vs = b.vs_baseline;
    if (b.key === "calls") return { note: "Baseline", cls: "base", text: "Selling at 2x, FOMO and the bot are compared with this book; the dossier rules with its scored calls" };
    // the verdict column says "Under audit"; this one gives the comparison it rests on
    if (AUDIT.has(b.key)) return { note: null, text: vs ? `${sgn(vs.value, 2)} points a trade against ${cmp(vs.against)}; entries timed before the calls existed` : "Entries timed before the calls existed" };
    if (b.unit === "equity" && b.benchmark_usd != null) return { note: null, text: `${usd(b.pnl_usd)} against ${usd(b.benchmark_usd - (b.capital_usd || 10000))} for every ranked coin; no range until week 6` };
    if (b.unit === "equity") return { note: null, text: "No comparison for a long-and-short book" };
    if (!vs || vs.value == null) return { note: null, text: "No comparison set yet" };
    const ahead = vs.lo > 0, behind = vs.hi < 0;
    return { note: ahead ? "Ahead" : behind ? "Behind" : "Level", cls: ahead ? "ahead" : behind ? "behind" : "level",
      text: `${Math.abs(vs.value).toFixed(1)} points a trade ${vs.value > 0 ? "better" : "worse"} than ${cmp(vs.against)} · range ${sgn(vs.lo)} to ${sgn(vs.hi)}` };
  }
  function progress(b) {
    const words = b.verdict?.words || "";
    const parts = [...words.matchAll(/([\d,]+) of the ([\d,]+) (?:counted |closed )?(trades|ideas|days|weeks) needed/g)];
    if (!parts.length) return { text: "", frac: 1 };
    const n = (x) => Number(x.replace(/,/g, ""));
    return { text: parts.map((m) => `${m[1]} of ${m[2]} ${m[3]}`).join(", "), frac: Math.min(...parts.map((m) => n(m[1]) / n(m[2]))) };
  }

  function answer(books) {
    const n = books.length, down = books.filter((b) => b.pnl_usd < 0).length;
    const graded = books.filter((b) => ["making", "losing", "unclear"].includes(state_(b)));
    const making = graded.filter((b) => state_(b) === "making"), losing = graded.filter((b) => state_(b) === "losing").length;
    const unclear = graded.filter((b) => state_(b) === "unclear").length;
    const reads = books.map((b) => b.verdict?.first_read).filter(Boolean).sort(), first = reads[0];
    let title, sub;
    if (!graded.length) {
      title = "Not <em>yet</em>.";
      sub = `${cap(word(down))} of the ${n} paper books are down after costs, and none has enough history for a verdict.${first ? ` The first verdicts are due ${day(first)}.` : ""}`;
    } else if (making.length) {
      title = `Yes, for <em>${word(making.length)}</em>.`;
      sub = `${cap(word(making.length))} of the ${graded.length} books with a verdict ${making.length === 1 ? "is" : "are"} making money after costs (${making.map((b) => b.name).join("; ")}). The rest are losing money or can't tell yet.`;
    } else {
      title = "<em>No</em>.";
      sub = `Of the ${graded.length} books with a verdict, ${losing} ${losing === 1 ? "is" : "are"} losing money after costs${unclear ? ` and ${unclear} can't tell yet` : ""}. None is making money.`;
      const late = books.filter((b) => state_(b) === "too_early");
      if (late.length) sub += ` ${cap(word(late.length))} more get${late.length === 1 ? "s its" : " theirs"} later.`;
    }
    return { title, sub, down, n, first, reads };
  }
  function improvement(books) {
    const ahead = books.filter((b) => !AUDIT.has(b.key) && b.vs_baseline?.lo > 0).sort((a, b) => b.vs_baseline.lo - a.vs_baseline.lo);
    const b = ahead[0];
    if (!b) return null;
    const v = b.vs_baseline, pt = perTrade(b);
    return { book: b, text: `The clearest improvement so far is ${PHRASE[b.key] || `the rule “${b.name}”`}. It did ${v.value.toFixed(1)} points a trade better than ${cmp(v.against)} (range ${v.lo.toFixed(1)} to ${v.hi.toFixed(1)})${pt == null ? "" : ` and still ${pt < 0 ? "lost" : "made"} ${Math.abs(pt).toFixed(1)}% a trade`}.` };
  }

  // ─── rendering ────────────────────────────────────────────────────────────
  function renderBooks() {
    const d = H.books?.books;
    if (!d) return;
    const books = d.books, a = answer(books), imp = improvement(books);
    const factorRead = books.filter((b) => b.unit === "equity").map((b) => b.verdict?.first_read).filter(Boolean).sort()[0];
    document.dispatchEvent(new CustomEvent("homeanswer", { detail: { title: a.title, sub: a.sub } }));
    const tile = (k, v, s, tone) => `<div class="kpi" style="--tone:${tone || "var(--cream)"}"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${s}</div></div>`;
    $("#hm-facts").innerHTML = `<div class="kpis hm-kpis">
      ${tile("Paper books", `${a.down} <small>of ${a.n} down</small>`, "after fees, slippage and funding", a.down ? "var(--bear)" : "var(--good)")}
      ${tile("First verdicts", a.first ? day(a.first) : "–", factorRead ? `factor books ${day(factorRead)}` : "")}
      ${imp ? tile("Clearest improvement", `${sgn(imp.book.vs_baseline.value)} <small>points a trade</small>`, `${esc(PHRASE[imp.book.key] || imp.book.name)} against ${esc(cmp(imp.book.vs_baseline.against))}, range ${sgn(imp.book.vs_baseline.lo)} to ${sgn(imp.book.vs_baseline.hi)}; still ${esc(ptText(imp.book))} a trade`, "var(--bull)") : ""}
    </div>
    <p class="hm-stamp">Books as of ${esc(new Date(d.computed_at).toISOString().slice(11, 16))} UTC, written from the books by a fixed template. ${imp ? esc(imp.text) : ""}</p>`;

    // the standings: the baseline first, then the rules by how clearly they beat their comparison
    const lo = (b) => (b.vs_baseline?.lo ?? -Infinity);
    const deep = books.filter((b) => b.lane === "deep"), maj = books.filter((b) => b.lane !== "deep");
    const meme = [...deep.filter((b) => b.key === "calls"), ...deep.filter((b) => b.key !== "calls").sort((x, y) => lo(y) - lo(x))];
    const ORDER = ["factor_long", "factor", "strategist", "fade_quant"];
    const majors = [...maj].sort((x, y) => (ORDER.indexOf(x.key) + 1 || 99) - (ORDER.indexOf(y.key) + 1 || 99));
    // a book under audit gets no verdict: it is left out of the count and the dates
    const live = books.filter((b) => !AUDIT.has(b.key)), held = books.length - live.length;
    const reads = live.map((b) => b.verdict?.first_read).filter(Boolean).sort();
    const soon = reads.filter((r) => new Date(r) - new Date(reads[0]) <= 14 * 864e5), later = reads.filter((r) => !soon.includes(r));
    const dueLine = !live.some((b) => ["making", "losing", "unclear"].includes(state_(b)))
      ? `${held ? `${cap(word(live.length))} of the ${books.length} books are too early to call and ${held === 1 ? "one is" : `${word(held)} are`} under audit.` : `All ${books.length} books are too early to call.`} ${cap(word(soon.length))} get a verdict ${day(soon[0])}${soon.length > 1 && day(soon.at(-1)) !== day(soon[0]) ? `–${day(soon.at(-1)).split(" ")[1]}` : ""}${later.length ? `, ${later.length === 1 ? "one more" : `the other ${word(later.length)}`} ${day(later[0])}` : ""}.`
      : "";
    const row = (b) => {
      const c = comparison(b), p = progress(b), st = state_(b);
      const verdict = st === "too_early" ? `<div class="due"><b>${day(b.verdict?.first_read)}</b><span class="vbar"><i style="width:${Math.max(4, Math.round(100 * p.frac))}%"></i></span><small>${esc(p.text)}</small></div>`
        : `<span class="vword ${st}">${{ making: "Making money", losing: "Losing money", unclear: "Can't tell yet", audit: "Under audit" }[st]}</span>`;
      const res = b.unit === "equity" ? `${usd(b.pnl_usd)}<small>on $${(b.capital_usd || 10000).toLocaleString()}</small>` : `${usd(b.pnl_usd)}<small>${(b.closed || 0).toLocaleString()} ${b.unit === "R" ? "ideas" : "trades"}</small>`;
      return `<tr data-book="${esc(b.key)}" tabindex="0" title="Open ${esc(b.name)}"${b.key === "calls" ? ` class="baseline"` : ""}>
        <td class="left bname">${esc(b.name)}</td>
        <td class="num ${b.pnl_usd > 0 ? "up" : b.pnl_usd < 0 ? "down" : ""}">${res}</td>
        <td class="num ${perTrade(b) > 0 ? "up" : perTrade(b) < 0 ? "down" : "dim"}">${esc(ptText(b))}${b.unit === "R" ? `<small>R is the $100 each idea risks</small>` : ""}</td>
        <td class="left cmp"><div>${c.note ? `<span class="lean ${c.cls}">${esc(c.note)}</span>` : `<span class="lean none"></span>`}<span>${esc(c.text)}</span></div></td>
        <td class="left">${verdict}</td></tr>`;
    };
    $("#hm-standings").innerHTML = `<header class="panel-h"><h2>Standings</h2><span class="cat">rules sorted by how clearly they beat their comparison · open a row for its book</span></header>
      ${dueLine ? `<p class="hm-due">${esc(dueLine)}</p>` : ""}
      <div class="table-wrap bare"><table class="plain hm-stand"><thead><tr><th class="left">Book</th><th>After costs</th><th>A trade</th>
        <th class="left">Against its comparison, so far</th><th class="left">Verdict</th></tr></thead><tbody>
        <tr class="grp"><td colspan="5" class="left">Memecoins · $100 a trade, held 24 hours unless the rule says otherwise</td></tr>${meme.map(row).join("")}
        <tr class="grp"><td colspan="5" class="left">Majors</td></tr>${majors.map(row).join("")}</tbody></table></div>`;
    renderDates();
    renderFactor();
    renderStatus();
  }

  // ─── Decide: the memo's actions ─────────────────────────────────────────────
  const KIND = { investigate: ["Look into", "inv"], pause: ["Pause", "pause"], budget: ["Budget", "budget"], cull: ["Retire", "pause"],
    resume: ["Reinstate", "budget"], promote: ["Promote", "inv"], keep: ["Keep", "inv"] };
  const ACT = { pause: "Pause to a sample…", retire: "Retire…", reinstate: "Reinstate…", budget: "Set budget…" };
  // api/memo.py: what each act leaves the agent as (an act whose result is in place already isn't offered), the
  // kinds that get buttons, and controls.OFF_ENSEMBLE
  const RESULT = { pause: "paused", retire: "retired", reinstate: "active" };
  const KIND_OF = { pause: "pause", retire: "cull", reinstate: "resume", budget: "budget" };
  const ACTABLE = new Set(["pause", "cull", "resume", "budget"]);
  const OFF_BLEND = new Set(["probation", "paused", "retired"]);
  const RUNG_WORD = { active: "active", watch: "on watch", probation: "benched", paused: "paused", retired: "retired" };   // agents.js RUNG_PHRASE
  const offered = (a) => a.acts.filter((x) => a.rung !== RESULT[x] && !(a.rung === "retired" && (x === "pause" || x === "budget")));
  const rulesNote = (a) => `Rule-based, so it always runs in full. ${OFF_BLEND.has(a.rung)
    ? `While it is ${RUNG_WORD[a.rung]} its calls carry no weight in the blended call.` : "Its place on the ladder is set on the Agents page."}`;
  const openHref = (t) => (!t ? null : t.startsWith("book:") ? `#/book/${encodeURIComponent(t.slice(5))}` : "#/agents");
  const actedWords = (a) => {
    const x = a.acted, after = x.after || {};
    const what = x.action === "budget" ? `budget set to ${Math.round((after.scale ?? 1) * 100)}%` : { pause: "paused to a sample", retire: "retired", reinstate: "reinstated" }[x.action] || x.action;
    return x.undone_at ? `Undone ${ago(x.undone_at)}` : `Done ${ago(x.at)}: ${what}`;
  };
  function confirmBox(a) {
    const name = esc(a.name || a.target);
    const c = H.confirm;
    if (!c || c.idx !== a.idx) return "";
    const say = {
      pause: `Pause the <b>${name}</b>? It keeps running on a 10% sample at a tenth of its budget, still graded, and its calls stay out of the blended call.`,
      retire: `Retire the <b>${name}</b>? It stops until you reinstate it.`,
      reinstate: `Reinstate the <b>${name}</b>? It goes back to full budget and into the blended call.`,
      budget: `The <b>${name}</b>'s budget, as a share of its normal one:`,
    }[c.action];
    const seg = c.action === "budget" ? `<div class="seg hm-seg" role="radiogroup" aria-label="Budget">${[[1, "Full"], [0.5, "Half"], [0.25, "Quarter"]].map(([v, w]) =>
      `<button type="button" role="radio" aria-checked="${c.scale === v}" data-scale="${v}" class="${c.scale === v ? "on" : ""}">${w}</button>`).join("")}</div>` : "";
    return `<div class="hm-confirm" role="group" aria-label="Confirm"><p>${say}</p>${seg}
      <div class="hm-row"><button type="button" class="hm-btn primary" data-go-act="${esc(c.action)}"${H.busy ? " disabled" : ""}>${{ pause: "Pause to a sample", retire: "Retire", reinstate: "Reinstate", budget: "Set budget" }[c.action]}</button>
      <button type="button" class="hm-btn" data-cancel>Cancel</button></div>
      ${H.error && H.error.idx === a.idx ? `<p class="hm-err">${esc(H.error.msg)}</p>` : ""}</div>`;
  }
  function renderMemo() {
    const host = $("#hm-decide"), d = H.memo;
    if (!d) return;
    const m = d.memo;
    if (!m) { host.innerHTML = `<header class="panel-h"><h2>Decide</h2></header><p class="note">No CEO memo yet. It is written at 00:55 and 12:55 UTC.</p>`; return; }
    const rows = m.actions.map((a) => {
      const [kw, kc] = KIND[a.kind] || [a.kind, "inv"];
      const head = a.plain || `${a.name || a.target}.`;
      const live = a.acted && !a.acted.undone_at, acts = offered(a);
      const said = live ? "" : a.rules && ACTABLE.has(a.kind) ? rulesNote(a)
        : a.acts.length && !acts.length ? `Already ${RUNG_WORD[a.rung] || a.rung}.` : "";
      const btns = live ? (a.acted.can_undo ? `<button type="button" class="hm-btn" data-undo="${a.idx}"${H.busy ? " disabled" : ""}>Undo</button>` : "")
        : acts.length ? acts.map((x) => `<button type="button" class="hm-btn" data-act="${x}" data-idx="${a.idx}">${ACT[x]}</button>`).join("")
        : openHref(a.target) ? `<a class="hm-btn" href="${openHref(a.target)}">Open</a>` : "";
      return `<li class="${live ? "acted" : ""}">
        <span class="mk ${kc}">${esc(kw)}</span>
        <div class="mt"><b>${esc(head)}</b>${a.plain ? "" : `<span>${esc(a.why)}</span>`}
          ${a.plain ? `<details><summary>The memo's wording</summary><p>${esc(a.name || a.target)}. ${esc(a.why)}</p></details>` : ""}
          ${a.acted ? `<p class="hm-done">${esc(actedWords(a))}</p>` : ""}${said ? `<p class="hm-said">${esc(said)}</p>` : ""}
          ${confirmBox(a)}</div>
        <div class="hm-btns">${btns}</div></li>`;
    }).join("");
    // what earlier memos' buttons did, while it can still be undone (a memo comes every 12 hours, an undo lasts 24)
    const recent = (d.recent || []).map((x) => {
      const [kw, kc] = KIND[KIND_OF[x.action]] || [x.action, "inv"];
      return `<li class="acted"><span class="mk ${kc}">${esc(kw)}</span>
        <div class="mt"><b>${esc(x.name)}</b><p class="hm-done">${esc(actedWords({ acted: { action: x.action, after: x.after, at: x.acted_at } }))}, from the memo of ${esc(x.title)}</p></div>
        <div class="hm-btns"><button type="button" class="hm-btn" data-undo="${x.idx}" data-report="${x.report_id}"${H.busy ? " disabled" : ""}>Undo</button></div></li>`;
    }).join("");
    host.innerHTML = `<header class="panel-h"><h2>Decide</h2><span class="cat">CEO memo, ${esc(m.title)} · ${m.actions.length} ${m.actions.length === 1 ? "action" : "actions"} in its order</span></header>
      ${m.actions.length ? `<ul class="hm-acts">${rows}</ul>` : `<p class="note">The memo recommends nothing this time.</p>`}
      ${recent ? `<h3 class="hm-sub">Done from earlier memos</h3><ul class="hm-acts">${recent}</ul>` : ""}
      <p class="hm-foot">Each button asks before it acts, and a change can be undone for 24 hours. <a href="#/report/${m.id}" data-go="reports">Read the whole memo →</a></p>`;
    renderDates();
  }

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Albatross-Action": "1" }, body: JSON.stringify(body) });
    if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); return null; }
    if (!r.ok) {
      let msg = `the server answered ${r.status}`;
      try { const j = await r.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : msg; } catch {}
      throw new Error(msg);
    }
    return r.json();
  }
  tab.addEventListener("click", async (e) => {
    const t = e.target;
    const actB = t.closest("[data-act]"), go = t.closest("[data-go-act]"), undo = t.closest("[data-undo]"), seg = t.closest("[data-scale]");
    if (actB) { H.confirm = { idx: Number(actB.dataset.idx), action: actB.dataset.act, scale: 0.5 }; H.error = null; renderMemo(); return; }
    if (t.closest("[data-cancel]")) { H.confirm = null; H.error = null; renderMemo(); return; }
    if (seg) { H.confirm.scale = Number(seg.dataset.scale); renderMemo(); return; }
    const id = H.memo?.memo?.id;
    if (go && H.confirm && id != null && !H.busy) {
      H.busy = true; renderMemo();
      const idx = H.confirm.idx, before = H.memo;
      let failed = null;
      try {
        await post(`/api/memo/${id}/act`, { idx, action: H.confirm.action, ...(H.confirm.action === "budget" ? { scale: H.confirm.scale } : {}) });
        H.confirm = null; H.error = null;
      } catch (err) { failed = err; }
      H.busy = false;
      await loadMemo();
      if (failed) {      // the answer can be lost after the change went through: say what the page now shows
        const a = H.memo !== before ? H.memo?.memo?.actions?.[idx] : null;
        if (a?.acted && !a.acted.undone_at) { H.confirm = null; H.error = null; }
        else H.error = { idx, msg: `Couldn't do it: ${failed.message}.${a ? " Nothing was changed." : ""}` };
        renderMemo();
      }
      return;
    }
    if (undo && id != null && !H.busy) {
      H.busy = true; renderMemo();
      const rid = undo.dataset.report ? Number(undo.dataset.report) : id;
      let failed = null;
      try { await post(`/api/memo/${rid}/undo`, { idx: Number(undo.dataset.undo) }); } catch (err) { failed = err; }
      H.busy = false;
      await loadMemo();
      if (failed) alertLine(failed);
      return;
    }
    const tr = t.closest("tr[data-book]");
    if (tr) { state.book = tr.dataset.book; setTab("book"); }
  });
  tab.addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr[data-book]");
    if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); state.book = tr.dataset.book; setTab("book"); }
  });
  const alertLine = (err) => { $("#hm-decide .hm-foot")?.insertAdjacentHTML("beforebegin", `<p class="hm-err">Couldn't undo it: ${esc(err.message)}.</p>`); };

  // ─── dates, the factor shortlist, the status line ───────────────────────────
  function renderDates() {
    const host = $("#hm-dates"), books = H.books?.books?.books;
    if (!books) return;
    const reads = books.filter((b) => !AUDIT.has(b.key)).map((b) => ({ d: b.verdict?.first_read, b })).filter((x) => x.d).sort((x, y) => (x.d < y.d ? -1 : 1));
    const first = reads[0]?.d, soon = reads.filter((x) => new Date(x.d) - new Date(first) <= 14 * 864e5), later = reads.filter((x) => !soon.includes(x));
    const items = [];
    if (H.memo?.next_review) items.push([new Date(H.memo.next_review).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }), "Weekly agent review; statuses can change"]);
    if (soon.length) items.push([`${day(soon[0].d)}${day(soon.at(-1).d) !== day(soon[0].d) ? `–${day(soon.at(-1).d).split(" ")[1]}` : ""}`, `Verdicts on ${word(soon.length)} ${soon.length === 1 ? "book" : "books"}`]);
    const byDay = {};
    later.forEach((x) => { (byDay[x.d] ||= []).push(x.b); });
    Object.entries(byDay).forEach(([d, bs]) => items.push([day(d), bs.every((b) => b.unit === "equity") ? "Factor rank verdicts" : `Verdicts on ${bs.map((b) => b.name).join("; ")}`]));
    const fv = H.health?.fomo_verdict, why = (fv?.decision?.why || []).join(" "), m = why.match(/(\d+) of (\d+) FOMO tokens graded/);
    host.innerHTML = `<header class="panel-h"><h2>Dates to watch</h2></header>
      <ul class="hm-soon">${items.map(([d, w]) => `<li><b>${esc(d)}</b><span>${esc(w)}</span></li>`).join("")}</ul>
      ${m ? `<p class="note">The FOMO buy test has ${m[1]} of its ${m[2]} tokens graded.</p>` : ""}`;
  }
  function renderFactor() {
    const host = $("#hm-factor"), r = H.intel?.ranking, books = H.books?.books?.books || [];
    if (!r) return;
    const fl = books.find((b) => b.key === "factor_long"), h = fl?.history || {};
    const line = (x) => `<li title="${esc((x.reasons || []).join(" · "))}"><button type="button" data-sym="${esc(x.symbol)}"><i>#${x.rank}</i><b>${esc(x.symbol)}</b><span>${esc((x.reasons || [])[0] || "")}</span></button></li>`;
    const note = h.weeks ? `Back-tested over ${h.weeks} weeks after trading costs, the long-only book made ${sgn(h.return_pct)}% while every ranked coin ${h.benchmark_return_pct < 0 ? "lost" : "made"} ${Math.abs(h.benchmark_return_pct).toFixed(1)}%, ahead in ${h.up_pct}% of weeks. It was chosen after trying about ten factors on that same history, so only its live weeks count.${H.intel.record?.live_since ? ` Tracked live since ${day(H.intel.record.live_since)}` : ""}${fl?.verdict?.first_read ? `; first verdict ${day(fl.verdict.first_read)}.` : "."}` : "";
    host.innerHTML = `<header class="panel-h"><h2>Factor rank</h2><span class="cat">${esc(day(r.day))} · ${esc(r.n)} liquid coins · <a href="#/factor" data-go="factor">all ${esc(r.n)} →</a></span></header>
      <h4 class="hm-lab up">Strongest</h4><ul class="hm-fr">${r.strongest.slice(0, 5).map(line).join("")}</ul>
      <h4 class="hm-lab down">Weakest</h4><ul class="hm-fr">${r.weakest.slice(0, 5).map(line).join("")}</ul>
      ${note ? `<p class="note">${esc(note)}</p>` : ""}`;
  }
  function renderStatus() {
    const host = $("#hm-status"), parts = [];
    const jobs = H.health?.jobs;
    if (jobs) {
      const n = (s) => jobs.filter((j) => j.state === s).length, ok = n("ok"), late = n("late") + n("failing") + n("error"), hand = n("manual"), off = n("paused");
      parts.push(`<span><b class="${late ? "warn" : "ok"}">●</b> ${ok} of ${jobs.length} jobs OK${late ? `, ${late} late` : ""}${off ? `, ${off} paused` : ""}${hand ? `, ${hand} run by hand` : ""}</span>`);
      const f = (H.health.agent_spend_today || []).filter((a) => a.failures);
      const calls = f.reduce((s, a) => s + a.failures, 0);
      if (calls) parts.push(`<span><b class="warn">●</b> ${calls} failed ${calls === 1 ? "call" : "calls"} today across ${f.length} ${f.length === 1 ? "agent" : "agents"}</span>`);
    }
    const c = H.books?.books?.cost_24h;
    if (c) parts.push(`<span>API $${c.api_usd.toFixed(2)} in 24 h</span>`);
    const p = H.plan;
    if (p?.used_percent != null) parts.push(`<span>ChatGPT plan ${Math.round(p.used_percent)}% used${p.pace_line != null ? `, pace line ${p.pace_line}%` : ""}</span>`);
    host.innerHTML = parts.length ? `${parts.join("")}<a href="#/health" data-go="health">Ops →</a>` : "";
  }
  tab.addEventListener("click", (e) => { const b = e.target.closest(".hm-fr button[data-sym]"); if (b) openAsset(b.dataset.sym); });

  // ─── loading: each panel on its own, so a slow source never holds up the rest ──
  const loadBooks = guarded("the paper books", ["#hm-standings"], async () => { H.books = await get("/api/books"); renderBooks(); });
  const loadMemo = guarded("the CEO memo", ["#hm-decide"], async () => { H.memo = await get("/api/memo/latest"); renderMemo(); });
  const loadSide = async () => {
    const [intel, health, out] = await Promise.allSettled([get("/api/intel"), get("/api/health"), get("/api/output")]);
    if (intel.status === "fulfilled") H.intel = intel.value;
    if (health.status === "fulfilled") H.health = health.value;
    if (out.status === "fulfilled") H.plan = out.value?.plan || null;
    renderFactor(); renderDates(); renderStatus();
  };
  const load = () => { loadBooks().catch(console.error); loadMemo().catch(console.error); loadSide().catch(console.error); };
  document.addEventListener("viewchange", (e) => { if (e.detail.view === "home") load(); });
  document.addEventListener("refresh", (e) => { if (e.detail.view === "home" && !H.busy && !H.confirm) load(); });
  if (tab.classList.contains("on")) load();
})();
