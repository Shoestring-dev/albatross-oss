"use strict";
// System > Output (albatross/tracker.py over /api/output): what every agent and job actually produced, what it
// cost, how it has graded, the loop signs, the map of who reads whom, and the controls (pause, budget scale).
// Uses app.js helpers: $, esc, get, ago, utc, cap.
(() => {
  const tab = $("#tab-output");
  if (!tab) return;
  const S = { window: "24h", data: null, busy: new Set() };
  const int = (n) => (n == null || !isFinite(n) ? "–" : Math.round(n).toLocaleString());
  const money = (v) => (v == null ? "–" : v >= 100 ? `$${Math.round(v)}` : v >= 1 ? `$${v.toFixed(2)}` : v >= 0.001 ? `$${v.toFixed(3)}`
    : v > 0 ? "<$0.001" : "$0");
  const FLAG = { failing: "bad", spinning: "bad", repeating: "warn", overlap: "warn", idle: "calm", paused: "calm" };
  const SCALES = [[0, "Off"], [0.5, "½×"], [1, "1×"], [2, "2×"]];
  const HOPS = ["reads raw data only", "reads one model's output", "two model hops from raw data", "three model hops from raw data"];

  async function load() {
    S.data = await get(`/api/output?window=${S.window}`);
    render();
  }

  async function control(key, body) {
    S.busy.add(key);
    render();
    try {
      const r = await fetch(`/api/controls/${encodeURIComponent(key)}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Albatross-Action": "1" }, body: JSON.stringify(body) });
      if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); return; }
      if (!r.ok) throw new Error(`the server answered ${r.status}`);
      await load();
    } catch (e) {
      $("#out-note").textContent = `Couldn't change ${key}: ${e.message}. Nothing was changed.`;
    } finally {
      S.busy.delete(key);
      render();
    }
  }

  // calls per hour, a faint area; the output-to-calls ratio is in the numbers beside it
  function spark(series, key) {
    const pts = (series || []).map((p) => p[key] || 0);
    if (pts.length < 2) return `<span class="cat">–</span>`;
    const W = 92, H = 24, max = Math.max(...pts, 1);
    const xy = pts.map((v, i) => `${(i / (pts.length - 1)) * W},${H - 2 - (v / max) * (H - 4)}`);
    return `<svg class="out-spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
      <polygon points="0,${H} ${xy.join(" ")} ${W},${H}"/><polyline points="${xy.join(" ")}"/></svg>`;
  }

  const chips = (flags) => (flags || []).map((f) => `<span class="out-flag ${FLAG[f.kind] || "calm"}" title="${esc(f.detail)}">${esc(f.label)}</span>`).join("");

  function controls(key, paused, scale, withScale) {
    const busy = S.busy.has(key) ? " disabled" : "";
    const pause = `<button class="out-btn${paused ? " on" : ""}" data-key="${esc(key)}" data-pause="${paused ? 0 : 1}"${busy}
      title="${paused ? "Resume it" : "Skip its runs until resumed"}">${paused ? "Resume" : "Pause"}</button>`;
    if (!withScale) return pause;
    const sc = SCALES.map(([v, label]) => `<button class="out-scale${Math.abs(v - scale) < 1e-9 ? " on" : ""}" data-key="${esc(key)}"
      data-scale="${v}"${busy} title="Budget ${label === "Off" ? "off" : label}">${label}</button>`).join("");
    return `${pause}<span class="out-scales" role="group" aria-label="Budget">${sc}</span>`;
  }

  function outputs(a) {
    const o = Object.entries(a.outputs || {});
    if (!o.length) return a.measured ? `<b class="out-zero">nothing stored</b>` : `<span class="cat">${int(a.ok)} answers</span>`;
    return o.map(([label, n]) => `<b>${int(n)}</b> ${esc(label)}`).join("<br>");
  }

  // The ChatGPT plan (albatross/llm/codex.py): the week's allowance, the pacer, and how many calls it carried.
  function planLine(p) {
    if (!p) return "";
    const utc = (t) => new Date(t).toLocaleString("en-US", { timeZone: "UTC", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }) + " UTC";
    const week = p.used_percent == null ? "No reading of the weekly allowance yet."
      : `${Math.round(p.used_percent)}% of this week's allowance is used, counting everything on the account, yours included (read ${esc(ago(p.checked_at))}). `
        + `The agents use the plan while the week's total stays under ${Math.round(p.pace_line)}%: the line rises evenly through the week and stops ${p.reserve_pct}% short of the end, which is kept for you. Resets ${esc(utc(p.resets_at))}.`;
    const lane = p.recent ? ` Of the last ${int(p.recent.n)} calls by the GPT agents, ${int(p.recent.on_plan)} ran on the plan (latest ${esc(utc(p.recent.at))}).` : "";
    return `<b>ChatGPT plan.</b> ${week}${lane} In this window ${int(p.on_plan)} calls ran on the plan`
      + `${p.failed ? ` (${int(p.failed)} failed there and were retried on OpenRouter)` : ""}, using ${p.credits.toLocaleString()} credits, about ${money(p.credits_usd)} at OpenAI's credit price;`
      + ` ${int(p.on_openrouter)} calls on the same GPT models went through OpenRouter. Only the GPT agents use the plan.`;
  }

  function render() {
    const d = S.data;
    if (!d) return;
    const s = d.summary;
    tab.querySelectorAll("#out-window button").forEach((b) => b.classList.toggle("on", b.dataset.w === S.window));
    $("#out-kpis").innerHTML = [
      ["Model calls", int(s.calls), `${int(s.job_runs)} job runs`],
      ["Real output", int(s.outputs), "signals, reads, reports and scores stored"],
      ["Spend", money(s.spend_usd), `plus ${money(s.pool_usd)} of the Grok and ChatGPT allowances, at their list prices`],
      ["Loop signs", int(s.flags), s.flags ? "see below" : "none in this window"],
    ].map(([k, v, sub]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${esc(sub)}</div></div>`).join("");

    $("#out-plan").innerHTML = planLine(d.plan);

    $("#out-flags").innerHTML = d.flags.length
      ? `<h3>Loop signs</h3><ul class="out-flaglist">${d.flags.map((f) => `<li><span class="out-flag ${FLAG[f.kind]}">${esc(f.label)}</span>
          <b>${esc(f.label_who)}</b><span>${esc(f.detail)}</span></li>`).join("")}</ul>`
      : `<p class="out-clear">No loop signs in this window: every agent's calls left something behind, nothing is resending the same input, and no job is piling up runs.</p>`;

    const fresh = d.graph.loops.filter((l) => !l.known), name = (a) => d.graph.nodes.find((n) => n.id === a)?.label || a;
    const known = Object.values(d.graph.loops.filter((l) => l.known).reduce((acc, l) => {      // one card per explanation
      (acc[l.known] ||= { known: l.known, paths: [] }).paths.push(l.agents);
      return acc;
    }, {}));
    $("#out-loops").innerHTML = `
      ${fresh.length ? `<p class="out-bad">New loop: ${fresh.map((l) => esc(l.agents.join(" → "))).join("; ")}. The tests fail until it is fixed.</p>` : ""}
      ${known.map((k) => `<div class="out-loop">${k.paths.map((p) => `<b>${esc(p.map(name).join(" → "))} → back</b>`).join("<br>")}
        <p>${esc(k.known)}</p></div>`).join("")}
      ${d.graph.attention_loops.length ? `<p class="cat">Attention loops (who decides what gets read, not what it concludes): ${d.graph.attention_loops
        .map((l) => esc(l.agents.map((a) => d.graph.nodes.find((n) => n.id === a)?.label || a).join(" ↔ "))).join("; ")}.</p>` : ""}
      <div class="out-depth">${[0, 1, 2, 3].map((k) => {
        const ns = d.graph.nodes.filter((n) => Math.min(n.depth, 3) === k);
        return ns.length ? `<div><h4>${HOPS[k]}</h4><p>${ns.map((n) => `<span class="out-node ${n.kind}">${esc(n.label)}</span>`).join("")}</p></div>` : "";
      }).join("")}</div>`;

    $("#out-agents").innerHTML = `<thead><tr><th class="left">Agent</th><th>Calls</th><th class="left">Real output</th><th>Spend</th>
      <th>Per output</th><th>Repeats</th><th class="left">How it has graded</th><th class="left">Status</th><th class="left">Controls</th></tr></thead>
      <tbody>${d.agents.map((a) => `<tr>
        <td class="left"><b>${esc(a.label)}</b><span class="sub">${a.depth == null ? esc(a.agent) : esc(HOPS[Math.min(a.depth, 3)])}</span></td>
        <td>${int(a.calls)}<span class="sub">${spark(a.hourly, "calls")}</span></td>
        <td class="left">${outputs(a)}</td>
        <td>${a.cost_usd ? money(a.cost_usd) : a.pool_usd ? `${money(a.pool_usd)}<span class="sub">allowance</span>` : "free"}</td>
        <td>${a.cost_per_output == null ? "–" : money(a.cost_per_output)}</td>
        <td>${a.repeats ? `${int(a.repeats)}<span class="sub">${Math.round((a.repeats / Math.max(a.ok, 1)) * 100)}%</span>` : "0"}</td>
        <td class="left">${a.quality ? esc(a.quality.summary) : `<span class="cat">not graded yet</span>`}</td>
        <td class="left">${chips(a.flags) || `<span class="cat">ok</span>`}</td>
        <td class="left out-ctl">${controls(a.agent, a.paused, a.budget_scale, true)}</td></tr>`).join("")}</tbody>`;

    $("#out-jobs").innerHTML = `<thead><tr><th class="left">Job</th><th>Runs</th><th>Rows</th><th>Idle runs</th><th>Skipped</th>
      <th>Avg</th><th>Longest</th><th class="left">Last run</th><th class="left">Status</th><th class="left">Control</th></tr></thead>
      <tbody>${d.jobs.map((j) => `<tr><td class="left"><b>${esc(j.job)}</b></td><td>${int(j.runs)}</td>
        <td>${int(j.rows)}<span class="sub">${spark(j.hourly, "rows")}</span></td><td>${int(j.idle)}</td><td>${int(j.overlap)}</td>
        <td>${j.avg_s == null ? "–" : `${j.avg_s}s`}</td><td>${j.max_s == null ? "–" : `${j.max_s}s`}</td>
        <td class="left cat">${j.last_run ? ago(j.last_run) : "–"}</td><td class="left">${chips(j.flags) || `<span class="cat">ok</span>`}</td>
        <td class="left out-ctl">${controls(j.job, j.paused, 1, false)}</td></tr>`).join("") ||
        `<tr><td colspan="10" class="left cat">No runs recorded yet in this window: the history starts with this release.</td></tr>`}</tbody>`;
    $("#out-jobs-sum").textContent = `${int(d.jobs.length)} jobs, ${int(s.job_runs)} runs`;
  }

  tab.addEventListener("click", (e) => {
    const w = e.target.closest("#out-window button[data-w]");
    if (w) { S.window = w.dataset.w; loadGuarded().catch(console.error); return; }
    const p = e.target.closest("button[data-pause]");
    if (p) { control(p.dataset.key, { paused: p.dataset.pause === "1" }); return; }
    const sc = e.target.closest("button[data-scale]");
    if (sc) control(sc.dataset.key, { budget_scale: Number(sc.dataset.scale) });
  });
  const loadGuarded = guarded("the output report", ["#out-kpis"], load);      // app.js: shared loading and failure states
  const onView = (v) => { if (v === "output") loadGuarded().catch(console.error); };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => onView(e.detail.view));
  if (tab.classList.contains("on")) loadGuarded().catch(console.error);
})();
