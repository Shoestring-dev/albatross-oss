# Models: what runs where, and how to try another (2026-10-03)

| Layer | Model | Why |
|---|---|---|
| **Workers:** HTF, LTF, Flow and Macro analysts, News Scout, events_news, the Deep End take, the Strategist's shadow | **GPT-6 Luna** on OpenRouter, or on a ChatGPT plan through the Codex lane (below) when it is on | cheap models (2026-10-02/03). Luna at low effort wrote no reasoning tokens in any test and 3-10x less output than DeepSeek for the same job, and its answers passed every agent's own checks |
| **Leads:** the Strategist's critic, reports, the Oracle's weekly review, the CEO | **GPT-6.1 Sol**, on OpenRouter or the plan | few calls a day; Sol costs 20x Luna per token, so only here. Effort: critic and reports medium, the CEO high, the weekly review medium (2026-10-04) |
| **Shadows** (graded experiments, feeding nothing) | the Strategist on Luna and on Sol; the HTF, LTF and Flow analysts on Sol | Added 2026-10-04. Each runs on the same inputs as the real agent, is graded as its own source (`strategist@gpt-6.1-sol`, `htf_analyst@gpt-6.1-sol` ...) and answers whether a stronger model calls better. Set in data.keys: `STRATEGIST_SHADOW_MODELS`, `ANALYST_SHADOW_MODELS` |
| Strategist (live) | GPT-6 Luna since 2026-10-05 | It ran on DeepSeek until then, so its graded record and its paper book mix the two models from Oct 5; Luna had run beside it as `strategist@gpt-6-luna` since Oct 3 and passed its checks more often. The Sol shadow (`strategist@gpt-6.1-sol`) stays as the challenger |
| **Web research:** Oracle research, events_oracle, deep_fit, deep_reddit_scan | **GPT-5.5 with the plan's live web search** (since 2026-10-05), OpenRouter's GitHub-excluding search when the plan can't take a call | GPT-5.5 is the one model this Codex CLI gives a native search tool (the GPT-6 family reach search only through code mode, which stays off). Domain filters are ignored by the CLI (tested), so the search can reach GitHub on OpenAI's side; that was accepted for this lane. Each agent's citation gate now checks against the pages the search opened |
| Social Pulse | Grok (CLI, on an xAI subscription) | reads X |
| **jev_analyst**, **jev_scout**, the critic's second opinion | **TypeSafe Jev, direct API** | calibrated probabilities for pennies |
| **deep_fit** fallback | Gemini 3.8 Flash via OpenRouter + its web search tool (Perplexity engine, <= 3 searches) | used only when the plan can't take the call; it found the same-day story behind a meme with real URLs at ~$0.03 a report (albatross/deepend/fit.py) |
| **deep_take** (The Deep End's one-paragraph take) | GPT-6 Luna, all of a run's takes (up to 6, one per ticker) in one call | summarises the evidence card; every number must appear in its own card, no buy/sell language, each take comes back under its own card number and symbol and names no other card's token; token names reach the prompt trimmed to 32 plain characters; the step is capped at 120 s; one call for six takes costs a third of six calls |
| **deep_x** (The Deep End X Scout) | Grok (CLI, the same xAI subscription pool as Social Pulse) | per-coin X reads on the lean flags; paced in pool dollars |

## TypeSafe Jev

Jev is a "System One" decision model: a state (text/JSON) plus named typed questions (choice, score, yes/no)
in, typed answers with probabilities out, no free text. $0.042 per million input tokens, output free,
~0.15 s a call. Called directly at `https://api.typesafe.ai/v1/systemone` with `TYPESAFE_API_KEY` in
the environment or the Setup page. Client: `albatross/llm/typesafe.py`, logged to agent_runs.

Where it fits: jobs that are really classifications. Our text jobs (a thesis, a report, an evidence-cited
call) need a text model; Jev can't write them.
- **jev_analyst** (hourly at :36): per tracked coin, one call with the 1h/4h/1d chart features and three
  choice questions (strong down / down / flat / up / strong up over the next 6 candles). Stored as source
  `jev_analyst`, graded like everything else on direction and on coin picking. **Not in the ensemble**
  until its record earns it: its first answer was 97% sure of a 24h move from one snapshot, and confidence
  is not evidence. Measured first run: 206 calls on 69 coins in 1.4 s, $0.0051 (~$0.12/day hourly).
- **Critic second opinion**: Jev's keep/downgrade/veto probabilities for each idea over the inputs the critic
  saw, stored in the idea's evidence (`jev_critic`) so we can later check whether a high P(veto) marks losers.

- **jev_scout** (every 5 min, at :02:50 past each 5m candle): builds candidate trades from our own work
  (every tracked coin, 1h/4h/1d, long and short, stop 1.5 ATR, target 2R, 6-candle horizon; 5m and 15m too
  until 2026-10-02, now context only, see below) and asks
  Jev, per coin in one call, "does price reach the target before the stop?". The state carries the chart per
  timeframe, the Strategist's call, the ensemble, factor rank and upcoming events, plus the inputs that move
  between candles: Hyperliquid funding, open interest and its 1h/24h change, the coin's scored news of the
  last 3 hours, and its latest X read (summary, why it matters, crowding) with the market pulse. Each
  candidate is scored once, on the first run after its candle's features land (5m/15m features at :01:30,
  1h/4h at :08), so a run only calls Jev for coins with a new candle. Every candidate lands in `jev_scores`
  and is graded after its horizon by the backtester's simulator, taken or not, so calibration covers the
  whole distribution. Jev ranks candidates in the right order but states its odds 2-3x too high (31,698 graded
  by 2026-09-30: said 24%, 9% won; the raw-odds paper book closed 55 trades at -0.30R each), so every P is first
  corrected against its graded record (`albatross/agents/jev_calibration.py`: per timeframe, binned, shrunk toward
  the timeframe's rate, made monotone, refit after every grading pass; a thin timeframe borrows the nearest map).
  Recent grades count most (half-life 3 h on 5m and 15m, 2 days on 1h and up), because hit rates swing with the
  market within hours. The half-lives come from a walk-forward replay: `python -m albatross.agents.jev_replay`.
  `jev_scores` keeps both `p_win` (Jev's) and `p_cal` (used). Paper book, on the corrected P: P >= 0.35 and
  EV >= +0.2R after the coin's own round-trip cost (the grader's model: fees plus slippage for $10k, from 0.1% on
  BTC to 2% on the thinnest coins; a 2R target breaks even at 1/3 before costs), at most 3 new a run, 20 open, one
  per coin; written to the ledger as source `model`/`jev`, kept out of the ideas board and the reports. The
  corrected odds top out near 0.22, so nothing qualifies: the honest outcome until Jev's top bins earn more. At the 15-minute cadence the first scan scored 512 candidates on 70 coins in
  2.8 s for $0.0079; every 5 minutes with 5m trades it was ~60k candidates a day for about $3 (cap $8).
  **Since 2026-10-02:** candidates are 1h/4h/1d only (~3-4k a day, ~$0.20). 5m and 15m were 94% of the
  calls, every confidence band of theirs lost after costs over 85k graded candidates, and their corrected odds
  never reached the book. Jev still reads 5m and 15m in the state, so the calibration still describes its answers.
  The stream stays for calibration and for scoring new Jev versions on a sample (learning-loop Phase 2, V4).
  The same day the Jev analyst was retired (harmful on its 1h/4h/1d direction question) and the LTF and Flow
  analysts were paused early on the culling ladder (docs/PLAN-LEARNING-LOOP.md section 7).
  Calibration and the book: Track record, `GET /api/jev`.

To add it to the ensemble once graded: `albatross/strategy/ensemble.py` SOURCES, with a prior; its weight then
follows its measured edge like every other source.

## What we learned about OpenRouter's "Jev Router" (typesafe/jev-router)

Before the owner pointed us at the direct API we probed the OpenRouter router on six live Albatross jobs. It used
Jev to pick a model and reasoning effort, then forwarded: DeepSeek V4.1 Flash on five jobs (LTF analyst,
Strategist, report, Macro analyst, critic) and Gemini 3.8 Flash on the news extractor. No markup (billed =
upstream cost), but it loses DeepSeek's prompt-cache discount, and it never picked a stronger model. Quality
matched run-to-run noise (22/24 vs 24/24 Strategist checks; 2 vs 3 news events). Not worth routing through.

## Trying any OpenRouter model without a deploy

- `LLM_MODEL_OVERRIDES=agent=model,agent=model` in data.keys switches an agent's model (recreate ingest).
- `STRATEGIST_SHADOW_MODEL=<model>` runs a shadow Strategist on that model every hour with the same inputs,
  stored as source `strategist@<slug>` and graded, feeding nothing (no ideas, no ensemble). Budget
  `STRATEGIST_SHADOW_BUDGET_USD` (default $3/day). Off by default.
- agent_runs.model records the model asked for and, for a router, the one that answered (`a -> b`).

## The ChatGPT plan lane (Codex): optional, since 2026-10-03

Off unless `CODEX_ENABLED=true`. Check your plan's terms first: a personal subscription is for your own use, not for
serving other people. When it is on, agents whose model is `openai/gpt-6-luna` or `openai/gpt-6.1-sol` are called
through Codex on the ChatGPT plan first (`albatross/llm/codex.py`, called from `openrouter.structured`), and through OpenRouter on the same model when
the plan can't take the call. The prompt and the checks on the answer are the same in both lanes.

- **The gateway** (`deploy/codex_gateway.py`, systemd `albatross-codex`, user `codex`): one request = one
  `codex exec` run, the CLI's official headless mode. The agent's own instructions replace Codex's coding prompt,
  the answer must match the agent's schema in strict form, web search and every tool that can be switched off are
  off (the ~3.6k tokens of tool definitions left can't run anything: code mode is disabled and fails closed), the
  sandbox is read-only, nothing is saved, and each run gets an empty folder. Listens on a Unix socket only, mounted
  into `ingest`. 8 calls at a time; a call waits up to 90 s for a slot, or 5 s for the every-minute and
  every-5-minute jobs (the Deep End take, News Scout), which then go to OpenRouter instead. Check it with
  `sudo journalctl -u albatross-codex -n 50`; never run codex with `RUST_LOG=trace` (the log can hold the login token).
- **The plans container** (`deploy/subs/`, `docker compose --profile subscriptions up -d`) runs this same gateway
  for Docker installs, with Claude Code beside Codex. Sign in from the Setup page; the logins stay in the container's
  `subs_home` volume (`docker volume rm albatross_subs_home` removes them).
- **The Claude lane** (`AGENTS_ON=claude`, `CLAUDE_ENABLED`, `CLAUDE_MODEL` haiku, sonnet or opus) runs `claude -p` with
  every tool off and the agent's schema as structured output, one turn. It has no pacer: when the plan reports its
  limit, the lane rests until the reset time Claude names (or 15 minutes) and calls go to OpenRouter if a key is set.
  It has no web search, so research stays on the ChatGPT plan or OpenRouter.
- **Codex CLI pinned at 0.160.0** (the installer refuses another version). The safety rests on its flags, so after
  any upgrade rerun the canary before trusting it: one `codex exec` with the gateway's own flags (`gw.argv`) as the
  `codex` user, instructions telling the model to use any tool it has to read `~/.codex/auth.json` and answer only
  `{"could_read": bool}`, and print only the event types. On 2026-10-03 the model tried `functions.exec`, got "Code
  Mode is unavailable ... will fail closed", and answered false. The gateway also fails any run in which a command,
  file change, MCP call or web search shows up.
- **Overhead** (measured 2026-10-02): Codex's default prompt is 13.5k tokens a call; with these settings 4.0k, of
  which ~3.6k are those tool definitions. They are the same every call and cost a tenth when cached.
- **Cost** is counted in OpenAI's Codex credits (`agent_runs.credits`; 1 credit = $0.04; per 1M tokens Luna 2.5 in,
  0.25 cached, 12.5 out; Sol 50, 2.5, 250). The plan's weekly allowance is drawn in the same units but its size
  isn't published: the `codex_limits` readings against our logged credits measure it.
- **Budgets:** an agent's daily budget caps its OpenRouter dollars, as before. Its use of the plan has its own cap: 10
  times the budget at list price (credits at $0.04; `PLAN_BUDGET_MULTIPLE`), so heavy use of the plan never turns into
  spend, and a day the plan runs dry costs no more than it always could. A shadow's plan cap is its budget itself.
- **Web search on the plan** (`codex.search`, 2026-10-05): GPT-5.5 with live search; the gateway lists every page the
  search opened, and those pages are the evidence each agent's citation gate checks (a citation of a page it didn't
  open is dropped; on the Reddit scan, a thread it didn't open). Weaker than the evidence it replaces: an open is
  listed even when the page doesn't exist (tested), so it proves the model asked for the page in this call, not that
  the page said it; a Reddit "no discussion found" from the plan is stored as unverified, never served as a fact.
  A search that opened any GitHub page is discarded (its run marked failed, nothing stored) and the call goes to
  OpenRouter's GitHub-excluding search; GitHub links the model cites without opening are masked in what is stored.
  The model opens few of the pages it cites (the first calendar run on the plan kept 5 of 18 events), so every
  research answer on the plan that cites pages it didn't open gets a second short pass that opens them
  (`codex.confirm`, logged as `<agent>_confirm`). An open alone proves nothing (a made-up URL "opens" too), so the
  pass is a fresh call that reports, page by page, whether it loaded with real content and the page's own title; a
  page counts only when the gateway saw it opened and it loaded with a title (a Reddit thread's title must also
  agree with its URL slug). Calendar dry run with it: 14 of 14 events kept, 30 credits.
  Our Telegram group names stay out of the fit prompt (a page it opens could try to send them elsewhere). Research
  stops at 90% of the pace line (after the shadows at 80%, before the regular agents) and holds at most 2 of the 8
  slots. Cost: GPT-5.5's API price as credits (125 / 12.5 / 750 per 1M) plus 0.25 a search; a research call
  measured 2-6 credits. Canary on GPT-5.5 (2026-10-05): told to write a file and read ~/.codex/auth.json, it called
  no tool at all.
- **Experiments (shadows)** give way to the regular agents everywhere: they stop at 80% of the pace line, share 2 of
  the gateway's 8 slots (with research's 2, the regular agents always have 4), and rest on their own if one
  hits the plan's limit.
  A shadow runs only on the plan (skipped, never sent to OpenRouter) unless its model costs pennies there: a Luna
  shadow may fall back, capped at $2 a day (`openrouter.shadow_terms`). A shadow that finds no room skips its whole
  run, never part of it; a job's shadows together stop 40 minutes (Strategist) or 20 (analysts) after the job
  started, so they never run into the next pass, and its batches go in random order so a cut leaves a random sample. Budgets are per shadow at list
  price (`STRATEGIST_SHADOW_BUDGET_USD` $20, `ANALYST_SHADOW_BUDGET_USD` $10).
- **The pacer:** job `codex_limits` reads the plan's usage windows every 5 minutes (they count all use on the
  account, the account holder's own included). The lane stays open while the weekly window is under a line that spreads 95% of the
  week evenly (5% is kept for the account holder) plus a 5-point burst. It closes on a reading older than 20 minutes, no weekly
  window, the plan reporting its limit (then 15 minutes' rest), or any shorter window (some plans have none)
  reaching 95%. Closed means OpenRouter on the same model.
- **Failures:** a failed try on the plan is retried on OpenRouter, so it counts as the lane's failure, not the
  agent's (alert `llm_failures:chatgpt_plan`; the Output page's plan line).
- **Switch it off** without a deploy: `CODEX_ENABLED=false` in data.keys, then recreate `ingest` and `dashboard`.
  Everything goes to OpenRouter on the same models.
- **The CEO** (`albatross/agents/ceo.py`, 00:55 and 12:55 UTC, GPT-6.1 Sol at medium effort): reads graded records
  only (the paper books with their rules, the dossier report, the verdict of every graded agent and every model
  agent, or that it has none, each agent's cost and failures, the failed jobs, the allowance) and writes a memo with findings and at most five recommended actions
  (Research › Reports, "CEO memos"). It changes nothing. Every number it writes must match one in its inputs
  (rounding allowed: this checks the number exists, not that it is pinned on the right book); an action must name an
  id it was shown, and only a graded agent can be recommended for a cut, a pause or a promotion. What fails a check
  is left out and the memo says how much.
- Dots (OpenAI's always-on agents, launched 2026-09-29) were looked at and left out: one per account, GPT-6 Astra
  only, and no API, so Albatross can't call one.
