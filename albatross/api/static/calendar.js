"use strict";
// The events calendar: Research › Calendar, "Coming up" on Today and a coin's own events on its page, all from
// /api/calendar (albatross/api/calendar.py). Self-contained; uses app.js helpers ($, esc, get, guarded, ago,
// openAsset, state).
(() => {
  const C = { group: "all", major: false, days: 30, longShots: false, open: new Set() };
  // A market-priced event the market gives under 25% is a long shot: hidden unless asked for.
  const LONG_SHOT = 0.25;
  const longShot = (e) => e.status === "rumored" && e.probability != null && e.probability < LONG_SHOT;
  const inDays = (e) => Math.max(0, Math.round((new Date(e.starts_at) - Date.now()) / 864e5));
  const GROUPS = {
    all: ["All", null], listings: ["Listings", "listing,delisting"], launches: ["Token launches", "tge,airdrop"],
    unlocks: ["Unlocks", "unlock"], upgrades: ["Upgrades", "mainnet,upgrade"], macro: ["Macro", "macro"],
    other: ["Other", "governance,conference,other"],
  };
  const KIND = {
    tge: "Token launch", listing: "Listing", delisting: "Delisting", unlock: "Unlock", mainnet: "Mainnet",
    upgrade: "Upgrade", airdrop: "Airdrop", governance: "Vote", macro: "Macro", conference: "Conference", other: "Other",
  };
  const IMPORTANCE = { 5: "market-moving", 4: "major", 3: "notable", 2: "minor", 1: "minor" };
  const MONTH = { month: "long", timeZone: "UTC" };

  const utcDay = (d) => d.toISOString().slice(0, 10);
  function dayLabel(iso) {
    const today = utcDay(new Date()), d = new Date(`${iso}T00:00:00Z`);
    const diff = Math.round((d - new Date(`${today}T00:00:00Z`)) / 864e5);
    if (diff === 0) return "Today";
    if (diff === 1) return "Tomorrow";
    if (diff === -1) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  }
  // One group per day for exact/day events; week, month and quarter events get a group of their own.
  function groupOf(e) {
    const d = new Date(e.starts_at);
    if (e.precision === "exact" || e.precision === "day") return [utcDay(d), dayLabel(utcDay(d))];
    if (e.precision === "week") return [`w${utcDay(d)}`, `Week of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })}`];
    if (e.precision === "month") return [`m${utcDay(d)}`, `Sometime in ${d.toLocaleDateString(undefined, MONTH)}`];
    return [`q${utcDay(d)}`, `Sometime in Q${Math.floor(d.getUTCMonth() / 3) + 1} ${d.getUTCFullYear()}`];
  }
  const when = (e) => (e.precision === "exact" ? `${new Date(e.starts_at).toISOString().slice(11, 16)} UTC` : "all day");
  const dots = (n) => `<span class="cal-imp" title="Importance ${n} of 5: ${IMPORTANCE[n]}">${"●".repeat(n)}<span class="off">${"●".repeat(5 - n)}</span></span>`;
  const coins = (e) => (e.assets || []).map((a) => `<button class="cal-coin" data-sym="${esc(a)}">${esc(a)}</button>`).join("");
  function status(e) {
    if (e.status === "cancelled") return `<span class="cal-status cancelled">cancelled</span>`;
    if (e.status === "happened") return `<span class="cal-status done">happened</span>`;
    if (e.probability != null) return `<span class="cal-status odds" title="The price of the Polymarket market on this date">Polymarket ${Math.round(e.probability * 100)}% by this date</span>`;
    return e.status === "rumored" ? `<span class="cal-status">unconfirmed</span>` : "";
  }
  const srcCount = (e) => `${e.sources.length} source${e.sources.length === 1 ? "" : "s"}`;
  const safe = (u) => (/^https?:\/\//i.test(u || "") ? u : null);

  function row(e) {
    const open = C.open.has(e.id);
    const detail = open ? `<div class="cal-detail">
        ${e.summary ? `<p>${esc(e.summary)}</p>` : ""}
        <ul class="cal-sources">${e.sources.map((s) => `<li>${safe(s.url) ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)} ↗</a>` : `<b>${esc(s.name)}</b>`}
          ${s.excerpt ? `<q>${esc(s.excerpt)}</q>` : ""}<span class="cat">${s.seen_at ? `seen ${esc(ago(s.seen_at))}` : ""}</span></li>`).join("")}</ul>
      </div>` : "";
    return `<li class="cal-ev k-${esc(e.kind)} ${esc(e.status)}${open ? " open" : ""}" data-id="${e.id}" tabindex="0" aria-expanded="${open}">
      <div class="cal-time">${esc(when(e))}</div>
      <div class="cal-main">
        <div class="cal-top"><span class="cal-kind">${esc(KIND[e.kind] || e.kind)}</span><b class="cal-title">${esc(e.title)}</b></div>
        <div class="cal-meta">${dots(e.importance)} ${coins(e)} ${status(e)} <span class="cat">${srcCount(e)}</span></div>
        ${detail}
      </div></li>`;
  }

  async function loadCalendar() {
    const q = new URLSearchParams({ days_back: 2, days_ahead: C.days, min_importance: C.major ? 4 : 1 });
    if (GROUPS[C.group][1]) q.set("kind", GROUPS[C.group][1]);
    const d = await get(`/api/calendar?${q}`);
    document.querySelectorAll("#cal-kinds button").forEach((b) => {
      const n = b.dataset.group === "all" ? Object.values(d.counts || {}).reduce((a, x) => a + x, 0)
        : (GROUPS[b.dataset.group][1] || "").split(",").reduce((a, k) => a + (d.counts?.[k] || 0), 0);
      b.querySelector(".n").textContent = n ? ` ${n}` : "";
    });
    const shots = d.events.filter(longShot).length;
    const shown = C.longShots ? d.events : d.events.filter((e) => !longShot(e));
    const ls = $("#cal-longshots");
    ls.hidden = !shots;
    ls.querySelector(".n").textContent = `${shots} long shot${shots === 1 ? "" : "s"}`;
    const groups = new Map();
    for (const e of shown) {
      const [k, label] = groupOf(e);
      if (!groups.has(k)) groups.set(k, { label, items: [] });
      groups.get(k).items.push(e);
    }
    const today = utcDay(new Date());
    $("#cal-list").innerHTML = shown.length ? [...groups.entries()].map(([k, g]) =>
      `<section class="cal-day${/^\d/.test(k) && k < today ? " past" : ""}"><h3>${esc(g.label)}</h3><ul>${g.items.map(row).join("")}</ul></section>`).join("")
      : `<div class="cal-empty"><p><b>Nothing ${C.major ? "major " : ""}on the calendar for this filter.</b></p>
          <p class="cat">Listings arrive within 10 minutes of the exchange notice, Polymarket odds hourly, the news every 30 minutes and the Oracle's web sweep once a day.</p></div>`;
  }

  async function loadComing() {
    const host = $("#home-coming-body");
    if (!host) return;
    const d = await get("/api/calendar?days_back=0&days_ahead=60&min_importance=3");
    const all = d.events.filter((e) => e.status !== "cancelled" && !longShot(e));
    // The next 7 days; on a quiet week, the next few notable events further out, marked "in N days".
    const week = all.filter((e) => inDays(e) <= 7);
    const items = (week.length ? week : all).slice(0, 6);
    const later = !week.length;
    host.innerHTML = items.length ? `<div class="coming">${items.map((e) => {
      const [, label] = groupOf(e);
      return `<button class="coming-ev k-${esc(e.kind)}" data-go-cal="1">
        <span class="coming-when">${esc(label)}${later ? ` · in ${inDays(e)} days` : ` · ${esc(when(e))}`}</span>
        <span class="cal-kind">${esc(KIND[e.kind] || e.kind)}</span>
        <b>${esc(e.title)}</b>
        <span class="coming-meta">${dots(e.importance)}${e.probability != null ? ` · ${Math.round(e.probability * 100)}% on Polymarket` : ""}</span></button>`;
    }).join("")}</div>${later ? `<p class="cat coming-note">Nothing notable in the next 7 days; these are the next ones.</p>` : ""}`
      : `<p class="cat">Nothing notable scheduled in the next 60 days.</p>`;
  }

  async function coinEvents(sym) {
    const body = $("#coin-body");
    if (!body) return;
    let d;
    try { d = await get(`/api/calendar?asset=${encodeURIComponent(sym)}&days_back=7&days_ahead=60`); } catch { return; }
    if (state.coin !== sym) return;
    body.querySelector(".cal-coinpanel")?.remove();
    if (!d.events.length) return;
    const panel = document.createElement("article");
    panel.className = "panel cal-coinpanel";
    panel.innerHTML = `<header class="panel-h"><h2>Events</h2><a class="cat" href="#/calendar" data-go-cal="1">the calendar →</a></header>
      <ul class="cal-mini">${d.events.slice(0, 8).map((e) => `<li class="k-${esc(e.kind)} ${esc(e.status)}"><span class="cal-when">${esc(groupOf(e)[1])}</span>
        <span class="cal-kind">${esc(KIND[e.kind] || e.kind)}</span> ${esc(e.title)} ${status(e)}</li>`).join("")}</ul>`;
    const after = body.querySelector(".ev-coinline");
    after ? after.after(panel) : body.prepend(panel);
  }

  const calendarLoad = guarded("the calendar", ["#cal-list"], loadCalendar, () => [C.group, C.major, C.days]);
  const comingLoad = guarded("what's coming up", ["#home-coming-body"], loadComing);

  // ─── wiring ───────────────────────────────────────────────────────────────
  $("#cal-kinds").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-group]");
    if (!b) return;
    C.group = b.dataset.group;
    document.querySelectorAll("#cal-kinds button").forEach((x) => x.classList.toggle("on", x === b));
    calendarLoad();
  });
  $("#cal-window").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-days]");
    if (!b) return;
    C.days = Number(b.dataset.days);
    document.querySelectorAll("#cal-window button").forEach((x) => x.classList.toggle("on", x === b));
    calendarLoad();
  });
  $("#cal-major").addEventListener("change", (e) => { C.major = e.target.checked; calendarLoad(); });
  $("#cal-longshots input").addEventListener("change", (e) => { C.longShots = e.target.checked; calendarLoad(); });
  const toggle = (li) => {
    const id = Number(li.dataset.id);
    C.open.has(id) ? C.open.delete(id) : C.open.add(id);
    calendarLoad();
  };
  $("#cal-list").addEventListener("click", (e) => {
    const coin = e.target.closest(".cal-coin[data-sym]");
    if (coin) { openAsset(coin.dataset.sym); return; }
    if (e.target.closest("a")) return;
    const li = e.target.closest("li.cal-ev");
    if (li) toggle(li);
  });
  $("#cal-list").addEventListener("keydown", (e) => {
    if (e.target.closest("a, button")) return;       // links and coin chips handle their own keys
    const li = e.target.closest("li.cal-ev");
    if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(li); }
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-go-cal]")) { e.preventDefault(); setTab("calendar"); }
  });
  // Today's "Coming up" panel left with the redesign (2026-10-06): the calendar lives under Majors
  const onView = (v) => { if (v === "calendar") calendarLoad(); if (v === "home" && $("#home-coming-body")) comingLoad(); };
  document.addEventListener("refresh", (e) => onView(e.detail.view));    // every view change and every minute
  document.addEventListener("coinloaded", (e) => coinEvents(e.detail.symbol));
  onView(state.tab);
})();
