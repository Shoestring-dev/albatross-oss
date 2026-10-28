"use strict";
// Agents (albatross/api/agents.py over /api/agents): who reads whom, left to right by model steps from raw data,
// with each agent's model, last-24-hour output and cost, and how it has graded. Hovering (or focusing) a card
// traces what it reads and who reads it; a click opens its full record in the drawer.
// Uses app.js helpers: $, esc, get, guarded, setTab.
(() => {
  const tab = $("#tab-agents");
  if (!tab) return;
  const S = { data: null, lane: "all", focus: null };
  try { S.lane = localStorage.getItem("albatross.agents.lane") || "all"; } catch {}
  // One set of words: better / no better / worse than what the agent is compared with, named from its grade: a coin
  // flip with its own long/short mix for direction calls, breaking even (R after costs) for ideas and trades.
  const GRADE = {
    edge: ["Better than its comparison", "up", "var(--good)"], none: ["No better than its comparison", "dot", "var(--warning)"],
    behind: ["Worse than its comparison", "down", "var(--critical)"], early: ["Too early", "ring", "var(--muted)"],
    input: ["Not graded", "dash", "var(--muted)"],
  };
  const against = (g) => {
    const t = g?.text || "";
    if (/fifth/i.test(t)) return "the weakest fifth (back-test)";        // factor rank: graded on its back-test spread
    return /\bR\b|mean R/.test(t) ? (/veto/i.test(t) ? "its vetoes" : "breaking even") : "a coin flip";
  };
  const gradeWord = (g) => {
    const k = g?.kind || "input";
    if (k === "edge") return `Better than ${against(g)}`;
    if (k === "none") return `No better than ${against(g)}`;
    if (k === "behind") return `Worse than ${against(g)}`;
    return (GRADE[k] || GRADE.input)[0];
  };
  // the review's own sentence, its old verdict word dropped ("Harmful: −5.1 pts ..." → "−5.1 points ...")
  const gradeDetail = (t) => String(t || "")
    .replace(/^(harmful|no skill shown|earns its place|measured edge|edge|behind naive or losing|(no )?better than [^:]+|worse than [^:]+)[:,]\s*/i, "")
    .replace(/\bpts\b/g, "points");
  const GROUP = { api: "Pay-per-token API", jev: "Jev (TypeSafe)", cli: "Subscription CLI", code: "Code" };
  // the culling system's ladder (albatross/agents/cull.py): weak weekly reviews move an agent down it
  const RUNG = { active: "Active", watch: "Watched", probation: "Benched", paused: "Paused", retired: "Retired" };
  const OFF_ENSEMBLE = new Set(["probation", "paused", "retired"]);      // controls.OFF_ENSEMBLE
  const RUNG_PHRASE = { probation: "benched", paused: "paused", retired: "retired" };
  // The timeframes every ensemble source has been culled from (the API's summary.uncovered), on the drawers it concerns.
  function uncoveredLine(n) {
    const unc = S.data?.summary?.uncovered || {}, tfs = Object.keys(unc);
    const concerned = n.id === "ensemble" || n.id === "strategist" || (OFF_ENSEMBLE.has(n.rung) && n.read_by.includes("ensemble"));
    return tfs.length && concerned ? `<p class="note">No source with skill covers ${esc(tfs.join(" or "))} (${esc(Object.values(unc)[0])}), so the
      blended call and the Strategist make no call there.</p>` : "";
  }
  const RUNG_SAYS = {
    active: "Full budget, counted in the blended call.", watch: "One weak review: nothing changes yet.",
    probation: "Out of the blended call, half its budget.", paused: "A tenth of its budget, on a sample, so it can earn its way back.",
    retired: "Stopped until you reinstate it.",
  };
  // a rules model (the quant model) is code: no budget to halve or cut, it just leaves the ensemble
  const RUNG_SAYS_CODE = { probation: "Out of the blended call; it still runs, free, and is graded.",
    paused: "Out of the blended call; it still runs, free, and is graded." };
  const day = (t) => new Date(t).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const hover = matchMedia("(hover: hover)").matches;
  const int = (n) => (n == null ? "–" : Math.round(n).toLocaleString());
  const usd = (v) => (v >= 0.995 ? `$${v.toFixed(2)}` : v >= 0.005 ? `$${v.toFixed(2)}` : "<$0.01");
  const byId = (id) => S.data?.nodes.find((n) => n.id === id);
  const label = (id) => byId(id)?.label || id;

  function glyph(kind, size = 10) {
    const [, shape, color] = GRADE[kind] || GRADE.input, h = size, m = h / 2;
    const d = {
      up: `<path d="M${m} 1L${h - 1} ${h - 1}H1Z" fill="${color}"/>`,
      down: `<path d="M1 1H${h - 1}L${m} ${h - 1}Z" fill="${color}"/>`,
      dot: `<circle cx="${m}" cy="${m}" r="${m - 1}" fill="${color}"/>`,
      ring: `<circle cx="${m}" cy="${m}" r="${m - 1.5}" fill="none" stroke="${color}" stroke-width="1.5"/>`,
      dash: `<path d="M1 ${m}H${h - 1}" stroke="${color}" stroke-width="2"/>`,
    }[shape];
    return `<svg width="${h}" height="${h}" viewBox="0 0 ${h} ${h}" aria-hidden="true">${d}</svg>`;
  }

  const made = (n) => {
    const parts = Object.entries(n.outputs || {}).filter(([, v]) => v).map(([k, v]) => `<b>${int(v)}</b> ${esc(k)}`);
    return parts.join(", ") || "nothing stored";
  };
  function cost(n) {
    if (n.cost_usd) return `<b>${usd(n.cost_usd)}</b>`;
    if (n.pool_usd) return `<b>${usd(n.pool_usd)}</b> allowance`;
    return n.model.group === "cli" ? "allowance" : "<b>&lt;$0.01</b>";
  }
  function meta(n) {
    if (n.kind === "rules") return "code";
    if (!n.calls) return "no calls in 24 h";
    return `<b>${int(n.calls)}</b> ${n.calls === 1 ? "call" : "calls"} → ${made(n)} · ${cost(n)}`;
  }

  function card(n) {
    if (n.kind === "dest") {
      return `<article class="ag-node dest" tabindex="0" role="button" data-id="${esc(n.id)}" aria-label="${esc(n.label)}: open it">
        <h4>${esc(n.label)}</h4><p class="ag-where">${esc(n.role)} <span aria-hidden="true">→</span></p>
        <p class="ag-reads">From ${esc(n.reads.map(label).join(", "))}</p></article>`;
    }
    const g = n.grade || { kind: "input", text: "" }, m = n.model;
    const flags = [n.rung && n.rung !== "active" ? `<span class="ag-rung r-${esc(n.rung)}" title="${esc(RUNG_SAYS[n.rung] || "")}">${esc(RUNG[n.rung] || n.rung)}</span>` : "",
      n.paused ? `<span class="ag-flag">Paused</span>` : "",
      ...(n.flags || []).map((f) => `<span class="ag-flag" title="${esc(f.detail)}">${esc(f.label)}</span>`)].join("");
    return `<article class="ag-node${n.kind === "rules" ? " code" : ""}${n.rung ? ` rung-${esc(n.rung)}` : ""}" tabindex="0" role="button" data-id="${esc(n.id)}"
        aria-label="${esc(n.label)}, ${esc(m.name)}. ${esc(g.text)}. Open its record.">
      <div class="ag-top"><h4>${esc(n.label)}</h4><span class="ag-model"><i class="ag-dot g-${esc(m.group)}"></i>${esc(m.name)}</span></div>
      <p class="ag-role">${esc(n.role)}</p>
      <p class="ag-meta">${meta(n)}</p>
      <div class="ag-grade">${glyph(g.kind)}<span>${esc(["input", "early"].includes(g.kind) ? g.text : `${gradeWord(g)}: ${gradeDetail(g.text)}`)}</span></div>
      ${flags ? `<div class="ag-flags">${flags}</div>` : ""}
      ${n.reads.length ? `<p class="ag-reads">Reads ${esc(n.reads.map(label).join(", "))}</p>` : ""}
    </article>`;
  }

  function render() {
    const d = S.data;
    if (!d) return;
    const s = d.summary;
    $("#ag-stats").innerHTML = [
      `<li><b>${int(s.agents)}</b> agents and code steps</li>`,
      `<li><b>${int(s.calls)}</b> model calls → <b>${int(s.outputs)}</b> stored outputs</li>`,
      `<li><b>$${(s.spend_usd ?? 0).toFixed(2)}</b> API + <b>$${(s.pool_usd ?? 0).toFixed(2)}</b> of subscription allowance</li>`,
      `<li><b>${int(s.loops)}</b> content loops</li>`,
      `<li>${Object.keys(s.rungs || {}).length ? Object.entries(s.rungs).map(([r, k]) => `<b>${int(k)}</b> ${esc((RUNG[r] || r).toLowerCase())}`).join(", ")
        : "no agent culled"} · next review ${esc(day(s.next_review))}</li>`,
      `<li class="ag-when">last 24 hours · ${esc(ago(d.as_of))}</li>`].join("");
    tab.querySelectorAll("#ag-lanes button").forEach((b) => b.classList.toggle("on", b.dataset.lane === S.lane));
    $("#ag-legend").innerHTML = `
      <div class="set"><span class="k">Runs on</span>${Object.entries(GROUP).map(([k, v]) =>
        `<span class="item"><i class="ag-dot g-${k}"></i>${v}</span>`).join("")}</div>
      <div class="set"><span class="k">Graded</span>${Object.entries(GRADE).map(([k, [v]]) =>
        `<span class="item">${glyph(k)}${v}</span>`).join("")}</div>
      <div class="set wires"><span class="k">Wires</span>
        <span class="item"><svg width="26" height="8" aria-hidden="true"><path d="M1 4H25" class="lg-solid"/></svg>reads its conclusions</span>
        <span class="item"><svg width="26" height="8" aria-hidden="true"><path d="M1 4H25" class="lg-dash"/></svg>decides only what it looks at</span></div>`;
    const cols = d.columns.length, last = cols - 1;
    const lanes = d.lanes.filter((l) => S.lane === "all" || l.id === S.lane);
    const tmpl = `170px repeat(${cols - 2}, minmax(0, 1fr)) 156px`;
    $("#ag-map").innerHTML = `<svg class="ag-wires" id="ag-wires" aria-hidden="true"></svg>` + lanes.map((lane, li) => `
      <section class="ag-band" data-lane="${esc(lane.id)}" style="grid-template-columns:${tmpl}">
        <div class="ag-bandname"><h3>${esc(lane.name)}</h3><span>${esc(lane.sub)}</span></div>
        ${li === 0 ? d.columns.map((c) => `<p class="ag-colhead hd">${esc(c)}</p>`).join("") : ""}
        ${d.columns.map((c, ci) => `<div class="ag-col c${ci === 0 ? "raw" : ci === last ? "dest" : ci}">
          <p class="ag-colhead inline">${esc(c)}</p>
          ${ci === 0 ? (d.raw[lane.id] || []).map((r) => `<div class="ag-raw"><b>${esc(r.name)}</b><span>${esc(r.detail)}</span></div>`).join("")
            : d.nodes.filter((n) => n.lane === lane.id && n.col === ci).map(card).join("")}
        </div>`).join("")}
      </section>`).join("");
    draw();
    if (S.focus) trace(S.focus);
  }

  // wires: from a producer's right edge to its reader's left edge; attention runs dotted; drawn over the gaps
  function draw() {
    const svg = $("#ag-wires"), map = $("#ag-map");
    if (!svg || !S.data || !tab.classList.contains("on") || getComputedStyle(svg).display === "none") return;
    const box = map.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    const at = (id) => {
      const el = map.querySelector(`.ag-node[data-id="${CSS.escape(id)}"]`);
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { l: b.left - box.left, r: b.right - box.left, y: (b.top + b.bottom) / 2 - box.top };
    };
    svg.innerHTML = `<defs><marker id="ag-arrow" viewBox="0 0 8 8" refX="1" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0 0L8 4L0 8Z"/></marker></defs>` + S.data.edges.map((e, i) => {
      const a = at(e.from), b = at(e.to);
      if (!a || !b) return "";
      let d;
      if (a.r <= b.l) { const k = Math.max(26, (b.l - a.r) / 2); d = `M${a.r} ${a.y}C${a.r + k} ${a.y} ${b.l - k} ${b.y} ${b.l - 6} ${b.y}`; }
      else if (b.r <= a.l) { const k = Math.max(40, (a.l - b.r) / 2); d = `M${a.l} ${a.y}C${a.l - k} ${a.y} ${b.r + k} ${b.y} ${b.r + 6} ${b.y}`; }
      else { const x = Math.max(a.r, b.r), k = 24 + Math.min(40, Math.abs(b.y - a.y) / 6); d = `M${a.r} ${a.y}C${x + k} ${a.y} ${x + k} ${b.y} ${b.r + 6} ${b.y}`; }
      return `<path data-i="${i}" class="w-${e.kind}" d="${d}" marker-end="url(#ag-arrow)"/>`;
    }).join("");
  }

  function trace(id) {
    const map = $("#ag-map"), lit = new Set([id]);
    map.querySelectorAll(".ag-wires path[data-i]").forEach((p) => {
      const e = S.data.edges[+p.dataset.i], on = e.from === id || e.to === id;
      p.classList.toggle("lit", on);
      if (on) { lit.add(e.from); lit.add(e.to); }
    });
    S.data.edges.forEach((e) => { if (e.from === id || e.to === id) { lit.add(e.from); lit.add(e.to); } });
    map.querySelectorAll(".ag-node").forEach((n) => n.classList.toggle("lit", lit.has(n.dataset.id)));
    map.classList.add("tracing");
  }
  function untrace() {
    if (S.focus) return trace(S.focus);                 // the drawer's agent stays traced while it is open
    const map = $("#ag-map");
    map.classList.remove("tracing");
    map.querySelectorAll(".lit").forEach((n) => n.classList.remove("lit"));
  }

  // weightless: on the ensemble's own list, a source the culling system has taken out of it says so
  const chipList = (ids, kind = "node", weightless = false) => ids.length
    ? `<div class="ag-d-chips">${ids.map((id) => `<button type="button" class="ag-chip" data-${kind === "dest" ? "goto-view" : "goto"}="${esc(
      kind === "dest" ? byId(id)?.view || "" : id)}">${esc(label(id))}${weightless && OFF_ENSEMBLE.has(byId(id)?.rung) ? " · no weight" : ""}</button>`).join("")}</div>`
    : `<p class="note">None.</p>`;

  function open(id) {
    const n = byId(id);
    if (!n) return;
    if (n.kind === "dest") { setTab(n.view); return; }
    S.focus = id;
    trace(id);
    const g = n.grade || { kind: "input", text: "" }, lane = S.data.lanes.find((l) => l.id === n.lane);
    const runs = n.kind === "rules" ? `<p>Plain code: no model calls, nothing to spend.</p>` : `<dl class="ag-d-kv">
        <dt>Calls</dt><dd>${int(n.calls)}${n.failed ? ` <span class="cat">(${int(n.failed)} failed)</span>` : ""}</dd>
        <dt>Stored</dt><dd>${made(n)}</dd>
        <dt>Cost</dt><dd>${n.cost_usd ? `${usd(n.cost_usd)} of API spend` : n.pool_usd ? `${usd(n.pool_usd)} of the subscription allowance` : "under $0.01"}</dd>
        <dt>Model</dt><dd>${esc(n.model.name)}${n.model.slug ? ` <span class="cat">${esc(n.model.slug)}</span>` : ""}</dd>
        ${n.paused ? `<dt>Status</dt><dd>Paused on the Spend &amp; allowance page</dd>` : ""}
        ${n.budget_scale != null && n.budget_scale !== 1 ? `<dt>Budget</dt><dd>${n.budget_scale}× its default</dd>` : ""}
      </dl>`;
    $("#drawer-body").innerHTML = `<div class="ag-drawer">
      <p class="kicker">Agents · ${esc(lane?.name || "")} · ${esc(S.data.columns[n.col] || "")}</p>
      <h2>${esc(n.label)}</h2>
      <p class="ag-d-role">${esc(n.role)}</p>
      <div class="ag-d-grade ${esc(g.kind)}">${glyph(g.kind, 12)}<div><b>${esc(gradeWord(g))}</b><span>${esc(gradeDetail(g.text))}</span></div></div>
      <h3>Last 24 hours</h3>${runs}
      ${ladder(n)}
      ${(n.flags || []).length ? `<h3>Loop signs</h3><ul class="ag-d-flags">${n.flags.map((f) => `<li><b>${esc(f.label)}</b> ${esc(f.detail)}</li>`).join("")}</ul>` : ""}
      <h3>Reads</h3>${n.reads.length ? chipList(n.reads, "node", n.id === "ensemble") : `<p class="note">Raw data only.</p>`}
      ${uncoveredLine(n)}
      ${n.attention.length ? `<h3>Looks where these point <span class="cat">attention only</span></h3>${chipList(n.attention)}` : ""}
      ${n.self ? `<h3>Also reads its own</h3><p>${esc(n.self)}</p>` : ""}
      <h3>Read by</h3>${chipList(n.read_by)}
      ${OFF_ENSEMBLE.has(n.rung) && n.read_by.includes("ensemble") ? `<p class="note">The blended call gives it no weight while it is ${esc(RUNG_PHRASE[n.rung])}.</p>` : ""}
      ${n.lands.length ? `<h3>Lands in</h3>${chipList(n.lands, "dest")}` : ""}
      ${n.kind === "rules" ? "" : `<p class="note">Pause it or scale its budget on the <a href="#/output" data-goto-view="output">Spend &amp; allowance</a> page.</p>`}
    </div>`;
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }

  // Pulling its weight? Where the culling system has it, why, what the next review would say today, and the owner's say.
  function ladder(n) {
    if (!n.rung) {
      return n.kind === "rules" && ["ensemble", "factor_rank"].includes(n.id)
        ? `<h3>Pulling its weight?</h3><p class="note">Exempt from culling: ${n.id === "ensemble" ? "it is the blend the others are culled from" : "it is the base edge, judged by its own record"}.</p>` : "";
    }
    const r = n.review || {}, hist = (n.history || []).filter((h) => h.verdict !== "manual" || h.rung_before !== h.rung_after);
    const steps = Object.keys(RUNG).map((k) => `<span class="${k === n.rung ? "on" : ""}">${esc(RUNG[k])}</span>`).join("");
    const why = n.rung_reason ? `${n.rung_reason}${/[.!?]$/.test(n.rung_reason) ? "" : "."}` : "";
    const reason = n.rung_reason ? `${n.rung === "active" ? "Last review" : `${RUNG[n.rung]} since ${day(n.rung_since)}`}: ${esc(why)}` : "Not reviewed yet.";
    const acts = [n.rung !== "active" ? `<button type="button" class="ag-act" data-rung-set="active">Reinstate</button>` : "",
      n.rung !== "retired" ? `<button type="button" class="ag-act danger" data-rung-set="retired">Retire</button>` : ""].join("");
    return `<h3>Pulling its weight?</h3>
      <div class="ag-ladder" role="img" aria-label="On the culling ladder: ${esc(RUNG[n.rung])}">${steps}</div>
      <p class="ag-d-says">${esc((n.kind === "rules" && RUNG_SAYS_CODE[n.rung]) || RUNG_SAYS[n.rung] || "")} ${reason}</p>
      <p class="ag-d-now"><b>On today's evidence:</b> ${esc(r.text || "nothing to grade it by yet")}. Next review ${esc(day(S.data.summary.next_review))}.</p>
      ${hist.length ? `<ul class="ag-d-hist">${hist.slice(0, 4).map((h) => `<li><span>${esc(day(h.reviewed_at))}</span>${esc(h.verdict === "manual" ? "set by the owner" : h.verdict.replace("_", " "))}
        ${h.rung_before !== h.rung_after ? ` · ${esc(RUNG[h.rung_before] || h.rung_before)} → ${esc(RUNG[h.rung_after] || h.rung_after)}` : ""}</li>`).join("")}</ul>` : ""}
      <div class="ag-d-actions" data-agent="${esc(n.id)}">${acts}</div>`;
  }

  async function setRung(agent, rung) {
    const r = await fetch(`/api/agents/${encodeURIComponent(agent)}/rung`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Albatross-Action": "1" }, body: JSON.stringify({ rung }) });
    if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); return; }
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    S.data = await get("/api/agents");
    render();
    open(agent);
  }

  // the drawer closes from app.js (button, Escape, a view change): drop the trace with it
  new MutationObserver(() => {
    if (!$("#drawer").classList.contains("open") && S.focus) { S.focus = null; untrace(); }
  }).observe($("#drawer"), { attributes: true, attributeFilter: ["class"] });

  $("#drawer").addEventListener("click", (e) => {
    if (!tab.classList.contains("on")) return;
    const act = e.target.closest("[data-rung-set]"), box = e.target.closest(".ag-d-actions");
    if (act && box) {                                   // retire and reinstate ask once, inline
      const agent = box.dataset.agent, rung = act.dataset.rungSet, name = label(agent);
      box.innerHTML = `<p>${rung === "retired" ? `Retire the ${esc(name)}? It stops making calls until you reinstate it.`
          : `Reinstate the ${esc(name)}? It goes back to full budget and into the blended call; the next review judges it afresh.`}</p>
        <button type="button" class="ag-act${rung === "retired" ? " danger" : ""}" data-confirm="${esc(rung)}">${rung === "retired" ? "Retire" : "Reinstate"}</button>
        <button type="button" class="ag-act" data-cancel>Cancel</button>`;
      return;
    }
    const ok = e.target.closest("[data-confirm]");
    if (ok && box) {
      ok.disabled = true;
      setRung(box.dataset.agent, ok.dataset.confirm).catch((err) => {
        box.innerHTML = `<p class="note">Couldn't change it: ${esc(err.message)}. Nothing was changed.</p>`;
      });
      return;
    }
    if (e.target.closest("[data-cancel]") && box) { open(box.dataset.agent); return; }
    const v = e.target.closest("[data-goto-view]");
    if (v) { e.preventDefault(); if (v.dataset.gotoView) setTab(v.dataset.gotoView); return; }
    const a = e.target.closest("[data-goto]");
    if (a) {
      open(a.dataset.goto);
      $(`#ag-map .ag-node[data-id="${CSS.escape(a.dataset.goto)}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    }
  });
  const map = $("#ag-map");
  map.addEventListener("click", (e) => { const n = e.target.closest(".ag-node"); if (n) open(n.dataset.id); });
  map.addEventListener("keydown", (e) => {
    const n = e.target.closest(".ag-node");
    if (n && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(n.dataset.id); }
  });
  if (hover) {
    map.addEventListener("mouseover", (e) => { const n = e.target.closest(".ag-node"); if (n) trace(n.dataset.id); });
    map.addEventListener("mouseleave", untrace);
  }
  map.addEventListener("focusin", (e) => { const n = e.target.closest(".ag-node"); if (n) trace(n.dataset.id); });
  map.addEventListener("focusout", () => untrace());
  $("#ag-lanes").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-lane]");
    if (!b) return;
    S.lane = b.dataset.lane;
    try { localStorage.setItem("albatross.agents.lane", S.lane); } catch {}
    render();
  });

  async function load() {
    S.data = await get("/api/agents");
    render();
  }
  const loadGuarded = guarded("the agent map", ["#ag-map"], load);       // app.js: shared loading and failure states
  const onView = (v) => { if (v === "agents") loadGuarded().catch(console.error); };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => onView(e.detail.view));
  if ("ResizeObserver" in window) new ResizeObserver(() => draw()).observe(map);
  if (document.fonts?.ready) document.fonts.ready.then(draw);
  if (tab.classList.contains("on")) loadGuarded().catch(console.error);
})();
