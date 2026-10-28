"use strict";
// Results › Strategy lab (albatross/lab over /api/lab): every strategy the lab tested, what is on paper, and how many
// are tried each day. Paper only: nothing trades. Uses app.js helpers: $, esc, get, ago, guarded.
(() => {
  const tab = $("#tab-lab");
  if (!tab) return;
  const FAM = {
    meme: { name: "Memecoins", sub: "group calls, FOMO buys and the bot's entries, $100 a trade", per: "trade", n: "trades" },
    factor: { name: "Majors", sub: "the liquid Hyperliquid coins ranked against each other", per: "period", n: "periods" },
  };
  const STATUS = {
    queued: ["Queued", "muted"], rejected: ["Rejected", "muted"], survivor: ["Waiting for a slot", "warning"],
    paper: ["On paper", "warning"], champion: ["Champion", "good"], retired: ["Retired", "critical"],
  };
  const ORIGIN = { seed: "what we already run", random: "random", mutation: "variation", crossover: "cross", researcher: "researcher" };
  const chip = (s) => { const [w, t] = STATUS[s] || [s, "muted"]; return `<span class="jstate ${t}">${esc(w)}</span>`; };
  const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
  const pct = (v) => { if (v == null) return "–"; const a = Math.abs(v).toFixed(2); return `${+a === 0 ? "" : sign(v)}${a}%`; };
  const tone = (v) => (v == null || v === 0 ? "" : v > 0 ? "up" : "down");
  const tval = (v) => (v == null ? "–" : (+v).toFixed(2));
  const int = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const day = (d) => (d ? new Date(String(d).length <= 10 ? `${d}T00:00:00Z` : d).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "–");

  // the strategy's name, and under it where it came from (and the researcher's one-line idea)
  function who(r) {
    const from = r.origin === "mutation" || r.origin === "crossover"
      ? `${ORIGIN[r.origin]} of ${r.parent_ids.map((p) => `#${p}`).join(" and ")}` : ORIGIN[r.origin] || r.origin;
    return `<div class="lab-name">${esc(r.name)}</div><div class="sub">#${r.id} · ${esc(from)}${r.idea ? ` · ${esc(r.idea)}` : ""}</div>`;
  }
  const num = (s, key) => `<span class="${tone(s?.[key])}">${pct(s?.[key])}</span>`;

  function paperTable(rows, f) {
    if (!rows.length) return "";
    return `<div class="table-wrap"><table class="plain lab-table"><thead><tr><th>Strategy</th><th class="opt">On paper since</th>
      <th class="num">${FAM[f].n}</th><th class="num">Per ${FAM[f].per}</th><th class="num">t</th>
      <th class="num">Baseline</th><th>Ruled on</th></tr></thead><tbody>${rows.map((r) => {
        const fw = r.forward || {}, base = fw.baseline || {};
        return `<tr><td>${who(r)}</td><td class="opt">${day(r.paper_at)}</td><td class="num" data-k="${FAM[f].n}">${int(fw.n)}</td>
          <td class="num" data-k="per ${FAM[f].per}">${num(fw, "mean")}</td><td class="num" data-k="t">${tval(fw.t)}</td><td class="num" data-k="baseline">${num(base, "mean")}</td>
          <td data-k="ruled on">${r.status === "champion" ? day(r.decided_at) : `${day(r.checkpoint)}${f === "meme" ? '<span class="sub"> and 100 trades</span>' : ""}`}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  function testedTable(rows, f, why) {
    if (!rows.length) return '<p class="note">Nothing tested yet.</p>';
    return `<div class="table-wrap"><table class="plain lab-table"><thead><tr><th>Strategy</th><th>Status</th>
      <th class="num opt">Older ${FAM[f].n}</th><th class="num">Per ${FAM[f].per}</th><th class="num">t</th><th class="num opt">Bar</th>
      <th class="num">Recent per ${FAM[f].per}</th>${why ? "<th>Why</th>" : ""}</tr></thead><tbody>${rows.map((r) => `<tr>
        <td>${who(r)}</td><td>${chip(r.status)}</td><td class="num opt">${int(r.discovery?.n)}</td>
        <td class="num" data-k="older">${num(r.discovery, "mean")}</td><td class="num" data-k="t">${tval(r.discovery?.t)}</td><td class="num opt">${tval(r.t_needed)}</td>
        <td class="num" data-k="recent">${num(r.holdout, "mean")}</td>${why ? `<td class="lab-why">${esc(r.reason || "")}<span class="sub"> · ${esc(ago(r.tested_at))}</span></td>` : ""}</tr>`).join("")}
      </tbody></table></div>`;
  }

  function family(f, d, rep) {
    const F = FAM[f], c = d.counts || {}, fr = rep?.families?.[f] || {};
    const tried = Object.entries(c).filter(([s]) => s !== "queued").reduce((a, [, n]) => a + n, 0);
    const base = fr.baseline?.holdout;
    const head = `<h3>${F.name} <span class="cat">${esc(F.sub)}</span></h3>
      <p class="lab-line">${int(tried)} tested${c.queued ? `, ${int(c.queued)} queued` : ""}. A new strategy needs a t of at least
        <b>${tval(fr.bar)}</b> on the older data (${day(fr.discovery?.[0])} to ${day(fr.discovery?.[1])}) and has to hold up on
        ${day(fr.holdout?.[0])} to ${day(fr.holdout?.[1])}, where the baseline (${esc(fr.baseline?.name || "–")}) made
        <span class="${tone(base?.mean)}">${pct(base?.mean)}</span> a ${F.per}.</p>`;
    const champs = d.champions.length ? `<h4>Champions</h4>${paperTable(d.champions, f)}` : "";
    const paper = `<h4>On paper <span class="cat">${d.paper.length} of ${rep?.slots?.[f] ?? "–"} slots</span></h4>${d.paper.length
      ? paperTable(d.paper, f) : '<p class="note">None yet: no strategy has passed both tests.</p>'}`;
    const retired = d.retired.length ? `<details class="fold sub-fold"><summary>Retired <span class="cat">the last ${d.retired.length}</span></summary>
      ${paperTable(d.retired, f)}<ul class="lab-reasons">${d.retired.map((r) => `<li>#${r.id}: ${esc(r.reason || "")}</li>`).join("")}</ul></details>` : "";
    return `<section class="lab-fam">${head}${champs}${paper}
      <details class="fold sub-fold" open><summary>Best tested <span class="cat">by t on the older data, once there are enough ${F.n}</span></summary>${testedTable(d.best, f, false)}</details>
      <details class="fold sub-fold"><summary>Latest tested <span class="cat">and why each failed</span></summary>${testedTable(d.recent, f, true)}</details>
      ${retired}</section>`;
  }

  // tested and survived per day, both families together, and which origins the survivors come from
  function volume(daily, origins) {
    const days = [...new Set(daily.map((r) => String(r.day).slice(0, 10)))].sort();
    const by = (dd, f, k) => daily.filter((r) => String(r.day).slice(0, 10) === dd && r.family === f).reduce((a, r) => a + r[k], 0);
    const rows = days.map((dd) => `<tr><td>${day(dd)}</td><td class="num">${int(by(dd, "meme", "tested"))}</td><td class="num">${int(by(dd, "meme", "survived"))}</td>
      <td class="num">${int(by(dd, "factor", "tested"))}</td><td class="num">${int(by(dd, "factor", "survived"))}</td></tr>`).reverse().join("");
    const org = origins.filter((o) => o.tested).map((o) => `<tr><td>${FAM[o.family]?.name || o.family}</td><td>${esc(ORIGIN[o.origin] || o.origin)}</td>
      <td class="num">${int(o.tested)}</td><td class="num">${int(o.survived)}</td><td class="num">${o.tested ? `${(100 * o.survived / o.tested).toFixed(1)}%` : "–"}</td></tr>`).join("");
    return `<details class="fold sub-fold"><summary>Tested each day <span class="cat">and where the survivors come from</span></summary>
      <div class="lab-two">
        <div class="table-wrap"><table class="plain lab-table"><thead><tr><th>Day</th><th class="num">Memecoin tested</th><th class="num">survived</th>
          <th class="num">Factor tested</th><th class="num">survived</th></tr></thead><tbody>${rows || '<tr><td colspan="5">Nothing yet.</td></tr>'}</tbody></table></div>
        <div class="table-wrap"><table class="plain lab-table"><thead><tr><th>Family</th><th>Made by</th><th class="num">Tested</th>
          <th class="num">Survived</th><th class="num">Share</th></tr></thead><tbody>${org || '<tr><td colspan="5">Nothing yet.</td></tr>'}</tbody></table></div>
      </div></details>`;
  }

  function render(d) {
    const rep = d.report;
    $("#lab-when").textContent = rep ? `last run ${ago(rep.computed_at)}` : "";
    const fams = d.families || {};
    if (!rep || !fams.meme) {
      $("#lab-stands").textContent = "The lab hasn't run yet. Its first run tests what we already run, then the first variations.";
      $("#lab-body").innerHTML = "";
      return;
    }
    const total = (k) => (fams.meme.counts[k] || 0) + (fams.factor.counts[k] || 0);
    const tested = Object.values(fams).reduce((a, x) => a + Object.entries(x.counts).filter(([s]) => s !== "queued").reduce((b, [, n]) => b + n, 0), 0);
    const today = new Date().toISOString().slice(0, 10);
    const todayN = d.daily.filter((r) => String(r.day).slice(0, 10) === today).reduce((a, r) => a + r.tested, 0);
    const champs = total("champion");
    $("#lab-stands").innerHTML = `<b>${int(tested)}</b> strategies tested so far, ${int(todayN)} of them today.
      ${int(total("paper"))} on paper, ${int(total("survivor"))} waiting for a slot, ${champs ? `<b>${int(champs)} champion${champs === 1 ? "" : "s"}</b>` : "no champion yet"}.`;
    $("#lab-body").innerHTML = family("meme", fams.meme, rep) + family("factor", fams.factor, rep) + volume(d.daily, d.origins);
  }

  async function load() { render(await get("/api/lab")); }
  const loadGuarded = guarded("the strategy lab", ["#lab-body"], load);       // app.js: shared loading and failure states
  const onView = (v) => { if (v === "lab") loadGuarded().catch(console.error); };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => onView(e.detail.view));
  if (tab.classList.contains("on")) loadGuarded().catch(console.error);
})();
