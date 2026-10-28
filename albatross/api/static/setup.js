"use strict";
// Ops > Setup (api/setup.py, api/subs.py, api/xsetup.py, api/telegram_setup.py): add keys, connect the ChatGPT and
// Claude plans, X and Telegram. Opens by itself on the first visit while nothing runs the agents.
// Uses app.js helpers: $, esc, ago, setTab, state.
(() => {
  const tab = $("#tab-setup");
  if (!tab) return;
  const S = { setup: null, subs: null, x: null, tg: null, chats: null, sel: null, filter: "", denied: null,
    busy: new Set(), note: {}, reveal: {}, editing: new Set(), poll: null };
  const CARDS = ["ai", "x", "telegram", "market", "memecoins"];

  // ─── data ──────────────────────────────────────────────────────────────────
  async function call(method, url, body) {
    const opt = { method, credentials: "same-origin", headers: { "X-Albatross-Action": "1" } };
    if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body || {}); }
    const r = await fetch(url, opt);
    if (r.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.hash)}`); throw new Error("sign in first"); }
    let data = null;
    try { data = await r.json(); } catch { /* an empty or non-JSON answer */ }
    if (!r.ok) throw Object.assign(new Error(detail(data) || `the server answered ${r.status}`), { status: r.status });
    return data;
  }
  const detail = (d) => (typeof d?.detail === "string" ? d.detail : Array.isArray(d?.detail) ? d.detail.map((x) => x.msg).join("; ") : null);
  const optional = (url) => call("GET", url).catch((e) => (e.status === 404 ? null : { error: e.message }));

  async function load() {
    try {
      S.setup = await call("GET", "/api/setup");
      S.denied = null;
    } catch (e) {
      if (e.status === 403) { S.denied = e.message; render(); return; }
      throw e;
    }
    [S.subs, S.x, S.tg] = await Promise.all([optional("/api/setup/subs"), optional("/api/setup/x"), optional("/api/setup/telegram")]);
    if (S.tg?.watch && !S.sel) S.sel = new Set(S.tg.watch.map((c) => String(c.id)));
    render();
    schedulePoll();
  }

  const item = (name) => {
    for (const g of S.setup?.groups || []) for (const i of g.items) if (i.name === name) return i;
    return null;
  };
  const group = (id) => S.setup?.groups.find((g) => g.id === id);
  const on = (name) => item(name)?.value === "true";

  async function act(key, fn, cards) {
    if (S.busy.has(key)) return;
    S.busy.add(key);
    delete S.note[key];
    render(cards);
    try {
      await fn();
    } catch (e) {
      S.note[key] = { ok: false, message: e.message };
    } finally {
      S.busy.delete(key);
      render(cards);
    }
  }

  function merge(updated) {          // one item back from a write: replace it in place
    for (const g of S.setup.groups) g.items = g.items.map((i) => (i.name === updated.name ? updated : i));
  }

  async function putKey(name, body, cards) {
    await act(name, async () => {
      const out = await call("PUT", `/api/setup/keys/${name}`, body);
      merge(out.item);
      if (out.token) S.reveal[name] = out.token;
      S.editing.delete(name);
      const input = tab.querySelector(`[data-input="${name}"]`);
      if (input) input.value = "";
      await refreshSide(name);
    }, cards);
  }

  // A key that changes what a connection card shows: reread that card's status too.
  async function refreshSide(name) {
    S.setup = await call("GET", "/api/setup");
    if (/^(AGENTS_ON|CODEX|CLAUDE)/.test(name)) S.subs = await optional("/api/setup/subs");
    if (/^(XAI|X_SEARCH)/.test(name)) S.x = await optional("/api/setup/x");
    if (/^TELEGRAM_API/.test(name)) S.tg = await optional("/api/setup/telegram");
  }

  // ─── pieces ────────────────────────────────────────────────────────────────
  const busy = (key) => (S.busy.has(key) ? " disabled" : "");
  const STATE = { missing: ["Not set", "off"], saved: ["Saved", "ok"], env: ["From .env", "env"] };
  function stateChip(i) {
    const [label, cls] = STATE[i.state] || STATE.missing;
    return `<span class="st-chip ${cls}">${label}${i.hint ? ` <code>${esc(i.hint)}</code>` : ""}</span>`;
  }
  function noteLine(key, test) {
    const n = S.note[key] || test;
    if (!n) return "";
    const when = n.at ? ` <span class="cat">· checked ${esc(ago(n.at * 1000))}</span>` : "";
    return `<p class="st-note ${n.ok ? "ok" : "bad"}" role="status">${esc(n.message)}${when}</p>`;
  }
  const link = (i) => (i.link ? ` <a class="st-get" href="${esc(i.link)}" target="_blank" rel="noopener noreferrer">Get one ↗</a>` : "");

  function control(i) {
    const n = i.name, env = i.state === "env", dis = env ? " disabled" : busy(n);
    if (i.kind === "bool") {
      return `<button class="st-switch${i.value === "true" ? " on" : ""}" role="switch" aria-checked="${i.value === "true"}"
        data-act="toggle" data-name="${n}"${dis} aria-label="${esc(i.label)}"><span></span></button>`;
    }
    if (i.kind === "choice") {
      return `<div class="st-seg" role="group" aria-label="${esc(i.label)}">${i.choices.map((c) =>
        `<button data-act="choice" data-name="${n}" data-v="${esc(c)}" class="${c === i.value ? "on" : ""}"
          aria-pressed="${c === i.value}"${dis}>${esc(CHOICE[c] || c)}</button>`).join("")}</div>`;
    }
    if (i.kind === "token") {
      const shown = S.reveal[n];
      return `${shown ? `<div class="st-reveal"><code>${esc(shown)}</code><button class="st-btn" data-act="copy" data-text="${esc(shown)}">Copy</button>
          <span class="cat">Shown once. Copy it into your program now.</span></div>` : ""}
        <div class="st-actions"><button class="st-btn" data-act="gen" data-name="${n}"${dis}>${i.state === "missing" ? "Make a token" : "Make a new token"}</button>
        ${i.state === "saved" ? `<button class="st-btn quiet" data-act="remove" data-name="${n}"${dis}>Remove</button>` : ""}</div>`;
    }
    const secret = ["secret", "url", "password"].includes(i.kind);
    if (env) return `<p class="cat">Set in .env or data.keys. Change it there.</p>`;
    if (secret && i.state === "saved" && !S.editing.has(n)) {
      return `<div class="st-actions">
        ${i.can_test ? `<button class="st-btn" data-act="test" data-name="${n}"${dis}>${S.busy.has(n) ? "Testing…" : "Test"}</button>` : ""}
        <button class="st-btn quiet" data-act="replace" data-name="${n}"${dis}>Replace</button>
        <button class="st-btn quiet" data-act="remove" data-name="${n}"${dis}>Remove</button></div>`;
    }
    const type = secret ? "password" : i.kind === "number" ? "number" : "text";
    const val = !secret && i.value != null ? ` value="${esc(i.value)}"` : "";
    const ph = i.kind === "url" ? "https://…" : i.kind === "password" ? "At least 8 characters" : secret ? "Paste it here" : "";
    return `<form class="st-form" data-form="${n}">
      <input data-input="${n}" type="${type}"${type === "number" ? ` step="any" min="0" inputmode="decimal"` : ""}${val}
        placeholder="${esc(ph)}" autocomplete="${i.kind === "password" ? "new-password" : "off"}" spellcheck="false"
        aria-label="${esc(i.label)}"${dis}>
      <button class="st-btn primary" type="submit"${dis}>${S.busy.has(n) ? "Saving…" : "Save"}</button>
      ${S.editing.has(n) ? `<button class="st-btn quiet" type="button" data-act="cancel" data-name="${n}">Cancel</button>` : ""}
      ${!secret && i.state === "saved" ? `<button class="st-btn quiet" type="button" data-act="remove" data-name="${n}"${dis}>Reset</button>` : ""}
    </form>`;
  }
  const CHOICE = { openrouter: "OpenRouter", chatgpt: "ChatGPT plan", claude: "Claude plan", auto: "Auto", api: "xAI API", cli: "Grok CLI" };

  function row(name) {
    const i = item(name);
    if (!i) return "";
    return `<div class="st-row" id="st-row-${name}">
      <div class="st-row-h"><label class="st-label">${esc(i.label)}</label>${["bool", "choice", "number", "text"].includes(i.kind) ? "" : stateChip(i)}</div>
      <p class="st-help">${esc(i.help)}${link(i)}</p>
      ${control(i)}
      ${noteLine(name, i.test)}
    </div>`;
  }
  function advanced(names, label = "More settings") {
    const rows = names.filter((n) => item(n)).map(row).join("");
    return rows ? `<details class="st-more"><summary>${label}</summary>${rows}</details>` : "";
  }
  const copyable = (cmd) => `<div class="st-cmd"><code>${esc(cmd)}</code><button class="st-btn" data-act="copy" data-text="${esc(cmd)}">Copy</button></div>`;

  // ─── cards ─────────────────────────────────────────────────────────────────
  function cardAI() {
    const g = group("ai"), sub = S.subs, gw = sub?.gateway;
    const lane = item("AGENTS_ON")?.value || "openrouter";
    let plans;
    if (!sub || sub.error) {
      plans = `<p class="cat">This install can't show your plans${sub?.error ? `: ${esc(sub.error)}` : ""}.</p>`;
    } else if (!gw?.reachable) {
      plans = `<div class="st-sub"><h3>ChatGPT and Claude plans</h3>
        <p class="st-help">Your plans run in a separate container that keeps their logins away from everything else. Start it on the machine running Albatross, then check again:</p>
        ${copyable(gw?.how || "docker compose --profile subscriptions up -d")}
        <div class="st-actions"><button class="st-btn" data-act="subs-recheck"${busy("subs")}>Check again</button></div>
        ${noteLine("subs")}</div>`;
    } else {
      plans = planChatGPT(sub.chatgpt) + planClaude(sub.claude);
    }
    return `<header class="panel-h"><h2>${esc(g.title)}</h2>${S.setup.ai_ready ? `<span class="st-chip ok">On</span>` : `<span class="st-chip off">Off</span>`}</header>
      <p class="st-blurb">${esc(g.blurb)}</p>
      ${row("AGENTS_ON")}
      ${lane !== "openrouter" && !planReady(lane) ? `<p class="st-warn">The ${lane === "chatgpt" ? "ChatGPT" : "Claude"} plan isn't connected and turned on yet, so the agents ${item("OPENROUTER_API_KEY")?.state === "missing" ? "won't run" : "use OpenRouter"} until it is.</p>` : ""}
      ${row("OPENROUTER_API_KEY")}
      ${plans}
      ${row("ALERT_DAILY_SPEND_USD")}
      ${advanced(["CODEX_RESERVE_PCT", "TYPESAFE_API_KEY", "LLM_MODEL_OVERRIDES"])}`;
  }
  const planReady = (lane) => (lane === "chatgpt" ? S.subs?.chatgpt?.signed_in && on("CODEX_ENABLED")
    : lane === "claude" ? S.subs?.claude?.signed_in && on("CLAUDE_ENABLED") : true);

  function planChatGPT(c) {
    if (!c) return "";
    const lg = c.login || {};
    let body;
    if (c.signed_in) {
      body = `<p class="st-status ok">Signed in${c.plan ? ` (${esc(c.plan)} plan)` : ""}.</p>${row("CODEX_ENABLED")}
        <div class="st-actions"><button class="st-btn quiet" data-act="gpt-signout"${busy("gpt")}>Sign out</button></div>`;
    } else if (lg.state === "pending" && lg.url) {
      body = `<p class="st-help">Open this page, sign in to ChatGPT and enter the code. This page updates when you're done.</p>
        <div class="st-device"><a href="${esc(lg.url)}" target="_blank" rel="noopener noreferrer">${esc(lg.url)} ↗</a>
        <code class="st-code">${esc(lg.code || "")}</code><button class="st-btn" data-act="copy" data-text="${esc(lg.code || "")}">Copy code</button>
        <button class="st-btn quiet" data-act="gpt-signout"${busy("gpt")}>Cancel</button></div>`;
    } else {
      body = `${lg.state === "failed" && lg.error ? `<p class="st-note bad">${esc(lg.error)}</p>` : ""}
        <div class="st-actions"><button class="st-btn primary" data-act="gpt-login"${busy("gpt")}>${S.busy.has("gpt") ? "Starting…" : "Sign in with ChatGPT"}</button></div>
        <details class="st-more"><summary>Signed in on another computer?</summary>
          <p class="st-help">Run <code>codex login</code> there, then paste the contents of <code>~/.codex/auth.json</code>. It's stored only in the plans container.</p>
          <form class="st-form col" data-form="gpt-authjson"><textarea data-input="gpt-authjson" rows="4" spellcheck="false" aria-label="auth.json"></textarea>
          <button class="st-btn primary" type="submit"${busy("gpt")}>Save</button></form></details>`;
    }
    return `<div class="st-sub"><h3>ChatGPT plan <span class="cat">${c.installed ? `Codex ${esc(c.version || "")}` : "Codex isn't installed in the container"}</span></h3>
      ${body}${noteLine("gpt")}</div>`;
  }

  function planClaude(c) {
    if (!c) return "";
    const body = c.signed_in
      ? `<p class="st-status ok">Signed in.</p>${row("CLAUDE_ENABLED")}${row("CLAUDE_MODEL")}
        <div class="st-actions"><button class="st-btn quiet" data-act="claude-signout"${busy("claude")}>Sign out</button></div>`
      : `<p class="st-help">On a computer with Claude Code, run <code>claude setup-token</code>, sign in, and paste the token it prints. It's stored only in the plans container.</p>
        <form class="st-form" data-form="claude-token"><input data-input="claude-token" type="password" placeholder="sk-ant-…" autocomplete="off" spellcheck="false" aria-label="Claude token"${busy("claude")}>
        <button class="st-btn primary" type="submit"${busy("claude")}>${S.busy.has("claude") ? "Checking…" : "Save"}</button></form>`;
    return `<div class="st-sub"><h3>Claude plan <span class="cat">${c.installed ? `Claude Code ${esc(c.version || "")}` : "Claude Code isn't installed in the container"}</span></h3>
      ${body}${noteLine("claude")}</div>`;
  }

  function cardX() {
    const g = group("x"), x = S.x;
    const lane = x && !x.error ? x.lane : null;
    const status = lane === "api" ? `<span class="st-chip ok">On · xAI API</span>` : lane === "cli" ? `<span class="st-chip ok">On · Grok CLI</span>`
      : `<span class="st-chip off">Off</span>`;
    const spend = x?.api?.key_set ? `<p class="st-status">Today: $${(x.api.spent_today_usd || 0).toFixed(2)} of the $${(x.api.cap_usd || 0).toFixed(2)} cap${x.api.model ? ` · ${esc(x.api.model)}` : ""}.</p>` : "";
    const cli = x?.cli?.host_worker_seen_at ? `The Grok CLI last answered ${esc(ago(x.cli.host_worker_seen_at))}.` : "No Grok CLI has answered yet.";
    return `<header class="panel-h"><h2>${esc(g.title)}</h2>${status}</header>
      <p class="st-blurb">${esc(g.blurb)}</p>
      ${row("XAI_API_KEY")}${spend}${row("X_SEARCH_VIA")}${row("XAI_DAILY_USD")}
      <details class="st-more"><summary>Using the Grok CLI instead</summary>
        <p class="st-help">Install the Grok CLI on the machine running Albatross, sign in to your SuperGrok plan, and run the host worker beside Docker: <code>uv run python -m albatross host</code>. ${cli}</p>
        ${["SOCIAL_WEEKLY_USD", "DEEP_X_WEEKLY_USD"].map(row).join("")}</details>`;
  }

  function cardTelegram() {
    const g = group("telegram"), alertsOn = item("TELEGRAM_ALERT_BOT_TOKEN")?.state !== "missing" && item("TELEGRAM_ALERT_PASSWORD_HASH")?.state !== "missing";
    return `<header class="panel-h"><h2>${esc(g.title)}</h2></header>
      <p class="st-blurb">${esc(g.blurb)}</p>
      <div class="st-sub"><h3>Alerts <span class="st-chip ${alertsOn ? "ok" : "off"}">${alertsOn ? "On" : "Off"}</span></h3>
        ${row("TELEGRAM_ALERT_BOT_TOKEN")}${row("TELEGRAM_ALERT_PASSWORD_HASH")}${advanced(["TELEGRAM_ALERT_CHAT_ID", "TELEGRAM_SHARED_BOTS"])}</div>
      <div class="st-sub"><h3>Calls from your groups ${listenerChip()}</h3>${listener()}</div>
      <div class="st-sub"><h3>Calls from your own program</h3>${row("CALL_FEED_TOKEN")}</div>`;
  }
  function listenerChip() {
    const l = S.tg?.listener;
    if (!S.tg || S.tg.error || !S.tg.signed_in) return `<span class="st-chip off">Off</span>`;
    return l?.running ? `<span class="st-chip ok">Listening · ${S.tg.watch.length} chat${S.tg.watch.length === 1 ? "" : "s"}</span>` : `<span class="st-chip warn">Not listening</span>`;
  }
  function listener() {
    const t = S.tg;
    if (!t || t.error) return `<p class="cat">This install can't connect Telegram${t?.error ? `: ${esc(t.error)}` : ""}.</p>`;
    const keysRows = row("TELEGRAM_API_ID") + row("TELEGRAM_API_HASH");
    if (!t.api_id_set || !t.api_hash_set) {
      return `<p class="st-help">Your own Telegram account reads the groups and channels you pick, and every token address posted there is logged as a call. Start with an API id and hash for your account.</p>${keysRows}`;
    }
    if (!t.signed_in) {
      const lg = t.login || {}, k = "tg";
      let form;
      if (lg.state === "code_sent") {
        form = `<p class="st-help">Telegram sent a code to your Telegram app${lg.phone_hint ? ` for ${esc(lg.phone_hint)}` : ""}. Enter it here.</p>
          <form class="st-form" data-form="tg-code"><input data-input="tg-code" inputmode="numeric" autocomplete="one-time-code" placeholder="12345" aria-label="Login code"${busy(k)}>
          <button class="st-btn primary" type="submit"${busy(k)}>Sign in</button>
          <button class="st-btn quiet" type="button" data-act="tg-restart"${busy(k)}>Start over</button></form>`;
      } else if (lg.state === "password_needed") {
        form = `<p class="st-help">This account has two-step verification. Enter your Telegram password.</p>
          <form class="st-form" data-form="tg-password"><input data-input="tg-password" type="password" autocomplete="current-password" aria-label="Telegram password"${busy(k)}>
          <button class="st-btn primary" type="submit"${busy(k)}>Sign in</button></form>`;
      } else {
        form = `${lg.state === "failed" && lg.error ? `<p class="st-note bad">${esc(lg.error)}</p>` : ""}
          <p class="st-help">Sign in with the phone number of your Telegram account. Telegram sends a code to your Telegram app.</p>
          <form class="st-form" data-form="tg-start"><input data-input="tg-start" type="tel" autocomplete="tel" placeholder="+1 555 010 0000" aria-label="Phone number"${busy(k)}>
          <button class="st-btn primary" type="submit"${busy(k)}>${S.busy.has(k) ? "Sending…" : "Send code"}</button></form>`;
      }
      return `${form}${noteLine("tg")}${advanced(["TELEGRAM_API_ID", "TELEGRAM_API_HASH"], "API id and hash")}`;
    }
    const acct = t.account ? `${esc(t.account.name)}${t.account.username ? ` (@${esc(t.account.username)})` : ""}` : "your account";
    const l = t.listener || {};
    const status = `<p class="st-status ${l.running ? "ok" : ""}">Signed in as ${acct}. ${esc(l.status || "")}${l.last_message_at ? ` Last message ${esc(ago(l.last_message_at))}.` : ""} ${l.calls_today ? `${l.calls_today} call${l.calls_today === 1 ? "" : "s"} today.` : ""}</p>`;
    return `${status}${chatPicker()}${noteLine("tg")}
      <div class="st-actions"><button class="st-btn quiet" data-act="tg-signout"${busy("tg")}>Sign out of Telegram</button></div>`;
  }
  function chatPicker() {
    const t = S.tg, picked = t.watch || [];
    if (!S.chats) {
      return `<p class="st-help">${picked.length ? `Reading ${picked.map((c) => esc(c.title)).join(", ")}.` : "No chats picked yet."}</p>
        <div class="st-actions"><button class="st-btn primary" data-act="tg-chats"${busy("tg-chats")}>${S.busy.has("tg-chats") ? "Loading your chats…" : picked.length ? "Change chats" : "Pick chats"}</button></div>${noteLine("tg-chats")}`;
    }
    const f = S.filter.toLowerCase();
    const list = S.chats.filter((c) => !f || c.title.toLowerCase().includes(f));
    return `<div class="st-picker">
      <input class="st-filter" data-filter type="search" placeholder="Filter ${S.chats.length} chats" value="${esc(S.filter)}" aria-label="Filter chats">
      <ul class="st-chats">${list.map((c) => `<li><label><input type="checkbox" data-chat="${esc(String(c.id))}"${S.sel.has(String(c.id)) ? " checked" : ""}>
        <span>${esc(c.title)}</span><span class="cat">${esc(c.kind)}${c.members ? ` · ${c.members.toLocaleString()}` : ""}</span></label></li>`).join("")
        || `<li class="cat">No chat matches.</li>`}</ul>
      <div class="st-actions"><button class="st-btn primary" data-act="tg-watch"${busy("tg-watch")}>Save ${S.sel.size} chat${S.sel.size === 1 ? "" : "s"}</button>
      <button class="st-btn quiet" data-act="tg-chats-close">Cancel</button></div>${noteLine("tg-watch")}</div>`;
  }

  function cardGroup(id) {
    const g = group(id);
    if (!g) return "";
    const main = g.items.filter((i) => !i.advanced).map((i) => row(i.name)).join("");
    return `<header class="panel-h"><h2>${esc(g.title)}</h2></header><p class="st-blurb">${esc(g.blurb)}</p>
      ${main}${advanced(g.items.filter((i) => i.advanced).map((i) => i.name))}`;
  }

  function intro() {
    const s = S.setup, first = s.first_run;
    return `${first ? `<h2 class="st-hello">Welcome to Albatross.</h2>` : ""}
      <p class="st-lead">${first ? "It already runs without any keys: market data, factor rank, memecoin prices and the calendar. Add what you have below to turn on the rest. Every key is optional, saves straight away and needs no restart. Setup stays under Ops." : "Every key here is optional and applies without a restart. A key set in .env wins over one saved here."}</p>
      ${first || !s.done ? `<div class="st-actions"><button class="st-btn" data-act="done">${first ? "Skip for now" : "Done"}</button></div>` : ""}`;
  }

  // ─── render ────────────────────────────────────────────────────────────────
  function render(cards) {
    if (S.denied) { tab.innerHTML = `<article class="panel st-card"><p class="st-lead">${esc(S.denied)}</p></article>`; return; }
    if (!S.setup) return;
    if (!tab.querySelector("#st-intro")) {
      tab.innerHTML = `<div class="st-intro" id="st-intro"></div>${CARDS.map((c) => `<article class="panel st-card" id="st-${c}"></article>`).join("")}`;
      cards = null;
    }
    const want = cards ? new Set(cards) : new Set(["intro", ...CARDS]);
    const draw = { intro, ai: cardAI, x: cardX, telegram: cardTelegram, market: () => cardGroup("market"), memecoins: () => cardGroup("memecoins") };
    for (const c of want) {
      const el = c === "intro" ? $("#st-intro") : $(`#st-${c}`);
      if (el) el.innerHTML = draw[c]();
    }
    nudge();
  }
  const cardOf = (name) => {
    for (const g of S.setup?.groups || []) if (g.items.some((i) => i.name === name)) return g.id;
    return null;
  };

  // A Home notice while nothing runs the agents and the first-run page was dismissed.
  function nudge() {
    const el = $("#st-nudge");
    if (!el || !S.setup) return;
    el.hidden = S.setup.ai_ready;
    el.innerHTML = `The AI agents are off. <a href="#/setup">Open Setup</a> to add an OpenRouter key or connect your ChatGPT or Claude plan.`;
  }

  // ChatGPT's device login finishes in another tab; check every 3 seconds while it's pending.
  function schedulePoll() {
    clearTimeout(S.poll);
    if (S.subs?.chatgpt?.login?.state !== "pending" || state.tab !== "setup") return;
    S.poll = setTimeout(async () => {
      S.subs = await optional("/api/setup/subs");
      render(["ai"]);
      schedulePoll();
    }, 3000);
  }

  // ─── actions ───────────────────────────────────────────────────────────────
  tab.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.act, n = b.dataset.name, cards = n ? [cardOf(n), "intro"] : null;
    if (a === "toggle") putKey(n, { value: on(n) ? "false" : "true" }, ["ai", "intro"]);
    else if (a === "choice") putKey(n, { value: b.dataset.v }, [cardOf(n), "intro"]);
    else if (a === "gen") putKey(n, { generate: true }, cards);
    else if (a === "replace") { S.editing.add(n); render(cards); tab.querySelector(`[data-input="${n}"]`)?.focus(); }
    else if (a === "cancel") { S.editing.delete(n); render(cards); }
    else if (a === "remove") act(n, async () => { merge((await call("DELETE", `/api/setup/keys/${n}`)).item); delete S.reveal[n]; await refreshSide(n); }, cards);
    else if (a === "test") act(n, async () => { merge((await call("POST", `/api/setup/keys/${n}/test`)).item); }, cards);
    else if (a === "copy") copy(b);
    else if (a === "done") act("done", async () => { await call("POST", "/api/setup/done", { done: true }); S.setup.done = true; S.setup.first_run = false; setTab("home"); }, ["intro"]);
    else if (a === "subs-recheck") act("subs", async () => { S.subs = await optional("/api/setup/subs"); }, ["ai"]);
    else if (a === "gpt-login") act("gpt", async () => { S.subs.chatgpt.login = await call("POST", "/api/setup/subs/chatgpt/login"); schedulePoll(); }, ["ai"]);
    else if (a === "gpt-signout") act("gpt", async () => { await call("DELETE", "/api/setup/subs/chatgpt"); await refreshSide("CODEX"); }, ["ai", "intro"]);
    else if (a === "claude-signout") act("claude", async () => { await call("DELETE", "/api/setup/subs/claude"); await refreshSide("CLAUDE"); }, ["ai", "intro"]);
    else if (a === "tg-restart") act("tg", async () => { S.tg.login = { state: "idle" }; }, ["telegram"]);
    else if (a === "tg-signout") act("tg", async () => { await call("DELETE", "/api/setup/telegram"); S.chats = null; S.sel = new Set(); S.tg = await optional("/api/setup/telegram"); }, ["telegram"]);
    else if (a === "tg-chats") act("tg-chats", async () => { S.chats = await call("GET", "/api/setup/telegram/chats"); S.sel = new Set(S.chats.filter((c) => c.watched).map((c) => String(c.id))); }, ["telegram"]);
    else if (a === "tg-chats-close") { S.chats = null; S.filter = ""; render(["telegram"]); }
    else if (a === "tg-watch") act("tg-watch", async () => {
      await call("PUT", "/api/setup/telegram/watch", { ids: [...S.sel].map(Number) });
      S.chats = null; S.filter = "";
      S.tg = await optional("/api/setup/telegram");
    }, ["telegram"]);
  });
  tab.addEventListener("change", (e) => {
    const c = e.target.closest("[data-chat]");
    if (c) { c.checked ? S.sel.add(c.dataset.chat) : S.sel.delete(c.dataset.chat); const s = tab.querySelector('[data-act="tg-watch"]'); if (s) s.textContent = `Save ${S.sel.size} chat${S.sel.size === 1 ? "" : "s"}`; }
  });
  tab.addEventListener("input", (e) => {
    if (!e.target.matches("[data-filter]")) return;
    S.filter = e.target.value;
    const pos = e.target.selectionStart;
    render(["telegram"]);
    const f = tab.querySelector("[data-filter]");
    if (f) { f.focus(); f.setSelectionRange(pos, pos); }
  });
  tab.addEventListener("submit", (e) => {
    const f = e.target.closest("[data-form]");
    if (!f) return;
    e.preventDefault();
    const k = f.dataset.form, v = f.querySelector("[data-input]")?.value ?? "";
    if (k === "gpt-authjson") act("gpt", async () => {
      const r = await call("POST", "/api/setup/subs/chatgpt/auth-json", { auth_json: v });
      if (!r.ok) throw new Error(r.error || "That didn't work.");
      await refreshSide("CODEX");
    }, ["ai"]);
    else if (k === "claude-token") act("claude", async () => {
      const r = await call("POST", "/api/setup/subs/claude/token", { token: v.trim() });
      if (!r.ok) throw new Error(r.error || "That token didn't work.");
      await refreshSide("CLAUDE");
    }, ["ai", "intro"]);
    else if (k === "tg-start") act("tg", async () => { S.tg.login = await call("POST", "/api/setup/telegram/start", { phone: v.trim() }); }, ["telegram"]);
    else if (k === "tg-code") act("tg", async () => { S.tg.login = await call("POST", "/api/setup/telegram/code", { code: v.trim() }); await afterLogin(); }, ["telegram"]);
    else if (k === "tg-password") act("tg", async () => { S.tg.login = await call("POST", "/api/setup/telegram/password", { password: v }); await afterLogin(); }, ["telegram"]);
    else putKey(k, { value: v }, [cardOf(k), "intro"]);
  });
  async function afterLogin() {
    if (S.tg.login?.state === "done") S.tg = await optional("/api/setup/telegram");
  }
  async function copy(b) {
    const label = b.dataset.label || (b.dataset.label = b.textContent);
    try {
      await navigator.clipboard.writeText(b.dataset.text);
      b.textContent = "Copied";
    } catch {
      const code = b.parentElement.querySelector("code");     // no clipboard: select it for the viewer to copy
      if (code) getSelection().selectAllChildren(code);
      b.textContent = "Selected";
    }
    setTimeout(() => { b.textContent = label; }, 1600);
  }

  // ─── wiring ────────────────────────────────────────────────────────────────
  const loadGuarded = guarded("Setup", ["#tab-setup"], load);
  const onView = (v) => {
    if (v === "setup") loadGuarded().catch(console.error);
    else { clearTimeout(S.poll); if (v === "home" && S.setup) nudge(); }
  };
  document.addEventListener("viewchange", (e) => onView(e.detail.view));
  document.addEventListener("refresh", (e) => { if (e.detail.view !== "setup") onView(e.detail.view); });

  // First visit: open Setup while nothing runs the agents, unless the link asked for a particular page.
  call("GET", "/api/setup").then((s) => {
    S.setup = s;
    nudge();
    const asked = location.hash && !/^#\/?(home)?$/.test(location.hash);
    if (s.first_run && !asked && state.tab === "home") setTab("setup", { replace: true });
    else if (state.tab === "setup") load().catch(console.error);
  }).catch(() => { /* Setup isn't open to this visitor (another device with no site password): nothing to show */ });
})();
