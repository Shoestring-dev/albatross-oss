# The Deep End: spec v1 (2026-09-30)

The idea: there is no product that brings CoinGecko-style analytics, professional charts and
news to micro-cap memecoins. Trading "in the trenches" means building evidence for an entry from a patchwork of
tools, with very little time before the price moves away. **The Deep End aggregates that evidence in one place,
fast.** The albatross is the mascot. The micro-cap lane ("The shallow end") becomes The Deep End.

The shortlist: transparency into a coin (bubblemap-style clusters, holder distribution), sentiment on X ("this is
huge"), mentions in other groups, influencer and pro-trader involvement, volume, and whether the coin *fits* the
current subculture (a deep-research product). Plus CryptoPanic news in the pipeline.

The product answers one question in seconds: **"what does the evidence say about this coin, right now?"** It
never pretends to predict; every item carries its source and age, and the evidence is graded against outcomes
before any of it is called predictive (see "Honesty rules").

## 1. What the trader sees

1. **Token dossier** `#/deep/<chain>/<address>` (the heart). Header (logo, ticker, chain, copy-CA, links, age,
   launchpad, copycat warning), the **evidence card** (checklist of good / warn / bad / unknown lines, a 0-100
   evidence score, the "Albatross take" paragraph), a **pro chart** (TradingView Lightweight Charts: candles,
   volume, markers for group calls, bot entries and notable X posts), then sections: Holders (distribution,
   top holders with tags, insider clusters), X (mood, velocity, notable accounts, posts), Groups (who called it,
   when, at what price, caller track record), Fit (culture research), Trades (top traders, buy/sell flow),
   Security (mint/freeze, LP, dev history, honeypot on EVM), News.
2. **The Deep End feed** (replaces "The shallow end" list): tokens on our radar (group calls, Birdeye trending
   and new listings, DexScreener boosts/profiles) with price, since-call, mini chart, calls, X heat, evidence
   score and red-flag count. Filters: chain, age, liquidity, "no red flags".
3. Later: Narratives (what's running), Callers leaderboard.

## 2. Agent hierarchy (24/7)

```
Level 0  SENSORS (deterministic, ingest container)
  S1 Call intake ......... Telegram listeners -> /api/ingest -> micro_calls           (exists)
  S2 Discovery ........... Birdeye trending + new listings, DexScreener profiles/boosts -> de_watch   [F]
  S3 Market .............. micro pipeline prices (exists) + Birdeye 1m OHLCV (all pools) + trade stats,
                           dex-paid / boosts -> micro_candles_1m (source 'birdeye'), de_market        [B]
  S4 Chain intel ......... Jupiter tokens v2 (batched), RugCheck, Birdeye security/holders/top traders,
                           honeypot.is (EVM) -> de_chain_intel, de_holder_snapshots                   [A]
  S5 News ................ existing feeds + CryptoPanic API (when keyed) -> news_items               [F]
Level 1  ANALYSTS (LLM)
  A1 X Scout ............. Grok CLI (host), per-token X reads for hot tokens, budgeted -> de_x_reads  [C]
  A2 Trenches pulse ...... the hourly Social Pulse gains a memecoin-meta search (host)                [C]
  A3 Fit researcher ...... OpenRouter model + web search over the X read and the token's own story
                           -> de_fit_reports (culture fit, meme origin, meta, originality)            [D]
Level 2  SYNTHESIS
  D1 Dossier composer .... checklist + evidence score + Albatross take (DeepSeek), for hot/warm tokens
                           every minute -> de_dossier (latest) + de_dossier_history (graded later)   [F]
Level 3  OVERSIGHT
  G1 Evidence grader ..... daily: dossier-at-call vs outcome (DEX ledger) -> which evidence matters;
                           caller scorecards                                                          [F]
  G2 Health .............. existing alerts engine; every new job registers a cadence                  (exists)
```

Heat decides cadence (albatross/deepend/watch.py): **hot** = first call < 2 h ago, or >= 2 calls in the last hour,
or the bot holds it, or trending now, or someone opened its dossier in the last 10 min; **warm** = any call or
discovery in the last 24 h; **cold** = the rest (refreshed only when opened).

## 3. Ownership: files, tables, functions

Each builder owns only its files. Shared plumbing (`albatross/deepend/{__init__,cfg,birdeye,watch,registry}.py`,
migration 019, the registry wiring in `__main__.py`, `alerts/rules.py`, `api/app.py`) is done; don't edit it,
ask for a change in your report instead.

| Owner | Module | Migration | Tables |
|---|---|---|---|
| A chain intel | `albatross/deepend/chain.py` | `020_deep_chain.sql` | `de_chain_intel` (latest per token), `de_holder_snapshots` (hypertable) |
| B market | `albatross/deepend/market.py` (+ Birdeye candles in `albatross/micro/pipeline.py`, coverage in `albatross/api/micro.py`) | `021_deep_market.sql` | `de_market` (latest per token) |
| C X intel | `albatross/deepend/xintel.py` (+ the trenches search in `albatross/agents/social_pulse.py`) | `022_deep_x.sql` | `de_x_reads` |
| D fit | `albatross/deepend/fit.py` | `023_deep_fit.sql` | `de_fit_reports` |
| E UI | `albatross/api/static/deep.js`, `deep.css`, `index.html`, `app.js`, `gallery.js`, `gallery.css`, `micro.js`, `micro.css`, art | none | none |
| F discovery, news, dossier | `albatross/deepend/discovery.py`, `dossier.py`, `albatross/api/deep.py` | `024_deep_dossier.sql` | `de_watch`, `de_dossier`, `de_dossier_history`, `de_caller_stats` |

Every module exposes, for the registry:
- `JOBS: dict[str, Callable[[], Awaitable[int]]]` (ingest container jobs; return rows written),
- `SCHEDULE: list[tuple[str, dict]]` (APScheduler cron kwargs per job),
- `HOST_JOBS` / `HOST_SCHEDULE` (host worker jobs that need the Grok/Claude CLIs),
- `CADENCE: dict[str, int]` (seconds; the alerts engine flags a job stale after 3 cadences).

The dossier composer (F) calls these **section functions**, each returning exactly the JSON shape in section 4 or
`None` when there is no data yet:

```python
chain.section_security(chain, address) -> dict | None      # "security"
chain.section_holders(chain, address) -> dict | None       # "holders"
chain.section_traders(chain, address) -> dict | None       # "traders"
market.section_market(chain, address) -> dict | None       # "market"
market.candles(chain, address, tf, start, end) -> dict     # the candles endpoint body
xintel.section_x(chain, address) -> dict | None            # "x"
fit.section_fit(chain, address) -> dict | None             # "fit"
discovery.section_news(chain, address, symbol, name) -> list[dict]
# batch helpers for the feed (one query for many tokens):
market.feed_rows(keys) -> dict[(chain, address), dict]; xintel.heat(keys) -> dict[(chain, address), dict]
```

`chain` is our chain id (`solana`, `base`, `bsc`, `ethereum`, `robinhood`), `address` as stored in micro_tokens.

## 4. API contract (F implements; E builds against `docs/deepend/fixtures/*.json`)

`GET /api/deep/feed?view=hot|new|called|trending&chain=` -> `{"now", "rows": [row]}`, row =
`{chain, address, symbol, name, logo, age_s, price, mcap, liquidity, change_h1, since_call_pct, peak_x,
n_calls, n_groups, calls_24h, first_call_at, x_heat: {mood, posts_h, label} | null, score: {value, good, warn,
bad, unknown} | null, red_flags: [label], flag_checks: {ran, of} | null, discovered_via: [..], spark: {t0, t1, v:
[...], call_price}}`

`flag_checks` (2026-10-06): how many of the eight red-flag checks every token gets (`dossier.CORE_FLAG_KEYS`) ran.
The UI says "no red flag" only when all eight ran; otherwise it leads with how many ran, and the No-red-flags filter
leaves the row out. The dossier carries the same object with the `missing` keys.

`GET /api/deep/token/{chain}/{address}` -> the dossier:
```
{token:    {chain, address, symbol, name, logo, launchpad, created_at, age_s, description,
            links: {x, telegram, website, dexscreener, birdeye, explorer, pumpfun}, same_name: n, copycat_of: {...}|null},
 market:   {price, price_ts, mcap, fdv, liquidity, change: {m5,h1,h6,h24}, volume: {m5,h1,h6,h24},
            txns: {h1: {buys, sells}, h24: {buys, sells}}, traders: {h1, h24}, net_buyers_h1, organic_score,
            organic_label, holders, holders_change_h1_pct, dex_paid: {paid, at}|null, boosts, as_of},
 call:     {first_at, first_group, first_price, since_pct, peak_x, peak_at, n_calls, n_groups, calls_24h,
            groups: [{name, n, first_at, caller_stats: {calls, hit_rate_2x, median_24h_pct}|null}]}|null,
 calls:    [{at, group, caller, intent, price, since_pct}],
 security: {mint_revoked, freeze_revoked, lp_locked_pct, rugcheck: {score, risks: [{name, level, description}]},
            insiders, top10_pct, dev: {address, launches, migrated, balance_pct}, transfer_fee,
            honeypot: {is_honeypot, buy_tax, sell_tax}|null, as_of},
 holders:  {count, change_h1_pct, top10_pct, top: [{owner, pct, tags: [insider|dev|lp|cex|smart|pool]}],
            clusters: [{label, wallets, pct, reason}], history: [{ts, count, top10_pct}], as_of},
 traders:  {top: [{owner, pnl_usd, volume_usd, tags}], smart_holders, as_of},
 x:        {status: fresh|stale|none|budget, as_of, mood, mood_label, posts_h, unique_authors, summary,
            why_it_matters, notable: [{author, followers, text, url, at, stance}], kols: [..],
            account: {handle, followers, created, name_changes}|null, history: [{ts, mood, posts_h}]},
 fit:      {status, as_of, score, label, summary, meme, meta, audience, originality, risks: [..],
            citations: [{title, url}]},
 news:     [{title, url, source, published_at, votes}],
 github:   {status: fresh|stale|pending|missing|empty, as_of, link, via, own (false: an account link, or an older repo
            that never names the coin; shown, never judged), not_own: account|older|null,
            repo: {name, url, description, homepage, language, license, topics, owner_type, created_at, fork,
                   parent, parent_stars, gone_at}|null,
            metrics: {stars, forks, watchers, open_issues, pushed_at, archived, disabled, commits_7d, commits_30d,
                      contributors, release: {tag, at}|null, activity_as_of}|null,
            change: {h24, d7}, signals: {age_at_launch_days, days_since_push, stars_per_day, velocity, shared_with},
            flags: [{key, level: good|warn|bad|info, text}], links: [{link, repo, status}]}|null
            (null when the token links no GitHub: metadata only, the repo itself is never downloaded;
             no GitHub-provided text (names, description, topics) is ever sent to a model, and the
             dashboard loads no image from GitHub: see albatross/github_guard.py),
 fomo:     {as_of (the newest event or holder snapshot), window_h, traders: [{handle, display_name, rank, board: 7d|30d,
            pnl_30d, top, action: buy|sell|trim|holds (sell = left it; trim = still holds), at, usd,
            usd_kind: fill|position|holding, realized_usd, holding_usd, position_pnl_usd, is_dev, clan,
            record: {graded, hit_2x, hit_rate_2x, median_24h_pct}|null}], n_traders, n_ranked, n_top, n_holders,
            buys, sells, buyers, sellers, theses: [{handle, rank, text, at, source: feed|holding}],
            clans: [{name, traders, top}], holders_as_of, holders_stale}|null
            (albatross/deepend/fomo.py, through the unofficial fomoapi.io; null when no FOMO trader held or traded
             it in window_h. Handles and theses are FOMO users' own words: escaped, never linked, never sent to
             a model)
 reddit:   {status: fresh|stale, as_of, found, mood (-5..5), mood_label, activity: none|few|active, activity_label,
            summary, why_it_matters, threads: [{url, title, subreddit, posted, score, comments, stance, notable}],
            independent (threads outside the coin's own subreddit),
            red_flags: [{kind: rug|scam|impersonation|other, detail, url}], citations_dropped}|null
            (albatross/deepend/reddit.py, read through Google search; null when never read or only blind or
             failed reads; found false means the search reached Reddit and found no thread)
 wallets:  {wallets, labels: {top_trader|sniper|insider|bot|deployer|exchange|service|launchpad|mass_launch: n},
            clusters: [{id, kind: cotrade|funding, size, here, bought, within_min, funder, labels}],
            early: {buyers, snipers, share, sniped_it}|null,
            funding: {holders, checked, funders_known, by_deployer: [..], shared: [{funder, holders, deployer_too}]}|null,
            top_holders: [{owner, pct, labels, first_funder}], as_of, findings: [..]}|null
            (albatross/deepend/wallets.py; null when no wallet around the token is known. insider counts this
             token's RugCheck insiders only; deployer counts deployers of other coins)
 checklist:[{key, label, status: good|warn|bad|unknown, value, detail, source, as_of, scored}]
            (github, fomo, reddit and wallets lines appear only when their section is not null, and carry
             scored false: shown with their red flags, never counted in score until graded),
 score:    {value, good, warn, bad, unknown, take, take_at},
 freshness:{market, security, holders, x, fit, github, fomo, reddit, wallets}}
```
`GET /api/deep/candles/{chain}/{address}?tf=1m|5m|15m|1h&minutes=N` ->
`{source, candles: [[t, o, h, l, c, v]], markers: [{t, kind: call|bot|x, label, price}], until}`.

## 5. Budgets and etiquette (hard rules)

- **Jupiter** key may be shared with another program: at most 1 request / 2 s from us, tokens v2 search
  batched up to 100 mints per call.
- **Birdeye** (paid plan): its monthly compute units, spread over the days, and its rps limit. Only through `deepend/birdeye.py`, which
  counts units per call into `de_api_usage` and refuses past `DEEP_BIRDEYE_CU_PER_DAY`.
- **RugCheck** (free): <= 1 request/s. **DexScreener** (free): <= 4 requests/s. **honeypot.is**: <= 1 request/s.
- **Grok CLI** (X, on an xAI subscription): one pool shared by Social Pulse and the X Scout, paced in
  dollars of the pool (agent_runs.output->>'subscription_equiv_cost_usd'); lean flags only (see
  agents/social_pulse.py GROK_ARGS: `--tools x_search`, deny list, short system prompt).
- **OpenRouter** (fit research, takes): per-agent daily budgets via `openrouter.structured(daily_budget_usd=)`.
- **CryptoPanic**: API only (Cloudflare blocks some datacenter addresses; the RSS feed is gone). Off until
  `CRYPTOPANIC_API_KEY` is set.

## 6. Honesty rules

- Every checklist line names its source and age; missing data is "unknown", never "good".
- The evidence score is a checklist tally until G1 has graded it; the UI says so.
- Past backtests on this lane (market-cap bands, 2026) found no filter that survives walk-forward:
  no "buy signal" language anywhere.

## 7. Recommendations beyond the shortlist

Dev/deployer history (serial launchers: Jupiter reports dev mints and migrations), bundle and sniper share at
launch, dex-paid and boost status, organic vs wash volume (Jupiter organic score), holder growth velocity, smart
money holding (Birdeye top traders and PnL leaders), copycat detection (same-name contracts, which one is
original), CEX listing watch, the token's own X account health (age, handle changes), Telegram community size,
caller track records from our own graded ledger, and alerts on watched coins.

Upgrades that would lift limits: a bigger SuperGrok plan (X reads scale with it), a CryptoPanic API plan, Birdeye
Starter or Premium once the Lite units are measured, Helius RPC for wallet-funding clusters (Chainstack's shared
nodes refuse getTokenLargestAccounts and heavy history calls), and more Telegram groups on the listeners.
