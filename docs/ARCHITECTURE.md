# Architecture

Albatross is one Python package (`albatross/`) run three ways: a 24/7 scheduler in Docker (`python -m albatross run`),
a web dashboard (`uvicorn albatross.api.app:app`), and an optional host worker for subscription CLIs
(`python -m albatross host`). Everything shares one TimescaleDB database.

```
 DOCKER: ingest (scheduler, ~80 jobs)                          DOCKER: dashboard (FastAPI + vanilla JS, :8787)
 ┌──────────────────────────────────────────────┐             ┌───────────────────────────────────────────┐
 │ ingest     candles, funding/OI, news, macro,  │             │ Home, Results, Memecoins, Majors, Alpha,  │
 │            on-chain, events, memecoin prices  │             │ Ops: every page reads the same database   │
 │ features   indicators per timeframe, factor   │             └───────────────────────────────────────────┘
 │            rank, sector roll-ups              │                              ▲
 │ agents     analysts, Strategist, News Scout,  │──── TimescaleDB ────────────┘
 │            reports, CEO memo (OpenRouter or   │        ▲
 │            your ChatGPT or Claude plan)       │        │
 │ grading    every call graded against price;   │ DOCKER (optional): subs, the ChatGPT and Claude plans
 │            paper books, strategy lab, alpha   │ HOST (optional): Grok CLI X reads (Social Pulse, X Scout)
 │ telegram   your groups' calls (listener)      │
 │ alerts     job health -> Telegram             │
 └──────────────────────────────────────────────┘
```

**Core principle: LLMs interpret numbers, they never compute them.** Deterministic code computes every indicator;
agents receive those values and must cite them. Every LLM call is logged in `agent_runs` with tokens and cost, under
a per-agent daily cap.

## Repo map

| Path | What |
|---|---|
| `albatross/__main__.py` | CLI and both schedulers: `run`, `host`, `job <name>`, `status`, `migrate`, `backfill`, `universe` |
| `albatross/config.py` | settings from the environment (pydantic-settings); never log values |
| `albatross/keys.py` | keys saved on the Setup page (table `app_settings`), applied to `settings` in place under the environment |
| `albatross/db.py` | asyncpg pool, `upsert()`, migration runner (migrations apply on `run` start) |
| `albatross/http.py` | shared httpx client, retry policy, `WeightLimiter` token bucket |
| `albatross/venues/` | one client per data source: Hyperliquid (primary perps source), OKX, Coinbase, CoinGecko, Coinalyze, Coin Metrics, DefiLlama, DexScreener, FRED, GeckoTerminal, Jupiter, Polymarket, SoSoValue, Upbit |
| `albatross/universe/` | the asset universe: 25 sectors mapped to CoinGecko categories, tiers T1–T3 |
| `albatross/ingest/` | candles (self-healing sync), market and derivatives snapshots, news (RSS + CryptoPanic), macro, on-chain; `health.py` wraps every job |
| `albatross/features/` | indicators (pure math), the quant bias score, per-timeframe features, walk-forward `replay` |
| `albatross/strategy/` | the ensemble (accuracy-weighted blend of every live source), factor rank, evidence |
| `albatross/agents/` | LLM agents: LTF/HTF/flow/macro analysts, Strategist and its critic, News Scout, reports, Social Pulse, the Oracle, the CEO memo, the strategy researcher, Jev; `cull.py` is the weekly agent review |
| `albatross/llm/` | OpenRouter client with cost logging and budgets; the ChatGPT (Codex) and Claude plan lanes; X search through the xAI API or the Grok CLI (`xsearch.py` picks); TypeSafe client |
| `albatross/grading/` | grades every signal and sector call against realized price |
| `albatross/backtest/` | trade-idea grading (fees, slippage, funding), paper books, scorecards |
| `albatross/ledger.py` | the append-only, hash-chained call ledger (`trade_calls`) and its verifier |
| `albatross/deepend/` | memecoins: discovery, watch list, dossiers with red-flag checks, chain and wallet analysis, X/Reddit/GitHub/FOMO intel |
| `albatross/micro/` | memecoin prices you can trust (pool resolution, consensus prices, reconciliation) and the memecoin call ledger |
| `albatross/events/` | events calendar: listings, unlocks, macro dates, Polymarket odds, news extraction |
| `albatross/lab/` | the strategy lab: strategy grammar, evolution, multiple-testing bar, paper slots |
| `albatross/alpha/` | the Alpha section: loss-avoidance checklist, factor edge tracking, live SKIP/PASS test |
| `albatross/alerts/` | health rules, Telegram bot, notifications |
| `albatross/telegram/` | the built-in Telegram listener: your own account reads the chats you pick and logs each token address as a call |
| `albatross/api/` | FastAPI routes and `static/` (the single-page dashboard); `setup.py` and its neighbours serve the Setup page, behind `guard.py` |
| `db/migrations/` | numbered SQL migrations |
| `deploy/` | server compose layer, Caddy gate, Codex gateway, host worker unit, example call-feed exporter |

## Data

TimescaleDB, pinned to `2.30.1-pg17` so dumps restore between machines. The main tables:

- Universe: `assets`, `categories`, `asset_categories`, `markets`.
- Time series (hypertables, compressed): `candles`, `deriv_snapshots`, `metric_series`, `features`.
- Calls and grading: `signals` keyed by `(scope, tf, source, as_of)` so recomputes are idempotent; `outcomes`;
  `trade_calls` (the hash-chained ledger) and `trade_call_outcomes`.
- Agents: `agent_runs` (every LLM call with tokens and cost), `reports`, `escalations`.
- Setup: `app_settings` (saved keys; treat backups like the keys), `telegram_listener` (the listener's state).
- Memecoins: `micro_calls`, `micro_trades`, `micro_tokens`, `micro_snapshots`, the `deep_*` dossier tables.
- Research: `factor_ranks`, `lab_strategies`, `paper_reports`, `events`.
- Ops: `ingest_health`, `schema_migrations`.

## How a call becomes evidence

1. A source makes a call: the quant score, an analyst, the Strategist, a Telegram group, a bot, a FOMO trader.
2. It is logged at a price we trust, at the time we saw it, into `signals` or the `trade_calls` ledger. Calls we
   saw late are marked late; history is never back-filled as if seen live.
3. The grader closes it against realized price after its horizon, with fees, slippage and funding for trade ideas.
4. Paper books turn graded calls into dollars after costs, with rules fixed before the results.
5. The weekly agent review (`agents/cull.py`) moves agents that don't beat a fair random baseline down a ladder
   (active, watch, probation, paused), cutting their budget and their weight in the ensemble, and returns them to
   active when they earn it. Retiring one is a manual step on the Agents page.

The strategy lab adds about ten strategies an hour (variations, crosses and random draws from the grammar in
`lab/meme.py` and `lab/factor.py`). The pass bar rises with the number tried, max(2, sqrt(2 ln N)), on clustered
t-statistics, and a survivor also needs a positive holdout at least as good as its baseline. Rules live in
`lab/run.py`'s docstring.

## Keys and the Setup page

Every key is optional. A value comes from, first match wins: the process environment (in Docker, `.env` and
`data.keys` through `env_file`), the same files read from the repo root outside Docker, a value saved on the Setup
page (`app_settings`), then the default in code. `keys.py` applies a saved value to `settings` in place, so code that
reads `settings.<field>` when it runs sees it without a restart; ingest and the host worker re-read the table every
30 seconds, and long-running tasks (the alerts bot, the Telegram listener) restart when one of their keys changes.
Read keys when a job runs, never at import.

The Setup API (`api/setup.py`) never sends a saved secret back: the page sees "set" and, for a long key, its last four
characters. `api/guard.py` lets it answer only to a signed-in visitor when a site password is set, and otherwise only
on the host names in `ALLOWED_HOSTS` (localhost by default), so a page on another site can't reach it through DNS
rebinding. Writes also need JSON, the `X-Albatross-Action` header and the same origin.

## LLM stack

Models are set per agent in code and can be swapped with `LLM_MODEL_OVERRIDES`. `docs/MODELS.md` lists what runs
where and what each costs. `AGENTS_ON` picks the lane: `openrouter` (each agent's own model), `chatgpt` or `claude`
(every agent on that plan, falling back to OpenRouter when the plan is out of allowance and a key is set).

- **The plans container** (`deploy/subs/`, `docker compose --profile subscriptions up -d`) runs the Codex CLI and
  Claude Code headless, each run with every tool off, in an empty working directory, as a non-root user. Their logins
  stay in its own volume; the app reaches it over a Unix socket in a shared volume. On a server the same gateway can
  run on the host instead (`deploy/install-codex-gateway.sh`). A pacer keeps a share of the ChatGPT plan's weekly
  allowance free for the account holder.
- **X search** (`llm/xsearch.py`): the xAI API with `XAI_API_KEY` from the ingest container, capped at
  `XAI_DAILY_USD`, or the Grok CLI with X search as its only tool on the host worker, sandboxed, paced in the
  SuperGrok pool's dollar equivalent. `deploy/grok-hardening.sh` pins the CLI's telemetry and trace upload off. Either
  way, every X post an answer cites must be one the search returned and fall inside the window.

Check your plan's terms before you use a plan lane for anything beyond your own use.

## Telegram

- **Alerts:** your own bot (`TELEGRAM_ALERT_BOT_TOKEN`). A chat joins by sending it the alerts password.
- **Calls from your groups:** sign in on the Setup page with an API id and hash from my.telegram.org. The sign-in runs in
  the dashboard (`api/telegram_setup.py`); the listener runs in ingest as a long-lived task, stores each token address
  posted in a watched chat as a call through `api/ingest.store` with source `telegram`, and writes its state to
  `telegram_listener`. The same chat and token count once per 24 hours. The saved session works like your login.
- **Calls from your own program:** `POST /api/ingest/calls` with `CALL_FEED_TOKEN` (`deploy/call-feed/README.md`).

## Deployment

- **Local:** `docker compose up -d --build`. The dashboard listens on 127.0.0.1:8787 with no login.
- **Server:** `docker compose -f docker-compose.yml -f deploy/compose.server.yml up -d --build` adds a Caddy gate
  (the app's `/auth/check` must say yes: a signed 30-day device cookie from `/login`, or Basic credentials for
  scripts) and a Cloudflare Tunnel (`deploy/tunnel.token`, gitignored). Set `SITE_PASSWORD_HASH` and
  `SITE_SESSION_SECRET`. In the Cloudflare dashboard, point the tunnel's public hostname at `http://gate:80`, never at
  `dashboard:8787`: the gate is what asks for the login. The tunnel sits on a network that can reach only the gate.
- **Host worker:** `deploy/albatross-host.service` runs `python -m albatross host` outside Docker, where the Grok CLI
  is logged in.

## Gotchas already paid for

- **asyncpg can't deduce a parameter used in two contexts** (`AmbiguousParameterError`, e.g. `$2` bare and inside a
  `CASE`): cast it at every use (`$2::timestamptz`). Unit tests mock the pool and can't see it; run the
  Postgres-backed tests (`ALBATROSS_TEST_DSN`) against a scratch database before deploying new SQL.
- **A module no test imports can take ingest down.** `tests/test_imports.py` imports every module;
  `python -m compileall -q albatross` is the two-second check.
- **Compose `env_file` order matters:** a later file overrides an earlier one even with an empty value. `data.keys`
  loads after `.env` for that reason.
- **OpenRouter keeps slow non-streamed requests alive with whitespace**, which resets httpx's read timeout forever.
  Every `openrouter.structured()` call has a hard `deadline_s`.
- **Grok can skip the X search and fabricate.** The evidence gate decodes each cited post id's timestamp. The
  headless envelope puts the answer in `text`, often after a draft JSON object; `cli.last_json_object()` handles it.
- **CoinGecko keyless** punishes bursts: the limiter uses `burst=1` and backs off on 429.
- **OKX lists tokenized stocks as USDT swaps.** Filter `instCategory == "1"`.
- **Hyperliquid `k`-prefixed perps** (kPEPE, kBONK) are 1000x units; `venues/hyperliquid.py` normalizes them. HL
  keeps only the latest 5,000 candles per interval (5m is about 17 days).
- **GeckoTerminal's keyless limit is 10 calls a minute per IP**, and its free candles lag (89 minutes at the median
  when measured). Memecoin charts use stored quotes past the last trusted candle.
- asyncpg rejects numpy scalars, and Postgres `avg()` of integers comes back as `Decimal`: cast before using.
- SoSoValue returns 403 to default library user agents; Coinalyze takes its key as the `api_key` query parameter.
- Hyperliquid's baseline funding (0.00125% an hour, 10.95% APR) is neutral, not crowding.
