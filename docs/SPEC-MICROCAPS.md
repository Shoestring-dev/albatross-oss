# Micro-cap & new-token lane — spec v0.1

**Added 2026-09-29.** Albatross also covers micro-cap and freshly launched tokens: the ones Telegram groups call
and memecoin trading bots buy, with data pipelines accurate enough to trust. Status: **M1 built** (§10); M2–M4 not
started. Build order is in §8; open questions in §9.

---

## 1. Why: what these tokens look like

A typical day of memecoin bot buys: most tokens are bought within about an hour of their first pool, many within ten
minutes, at market caps and liquidity in the low tens of thousands of dollars;
Solana pools across PumpSwap, Raydium, the pump.fun bonding curve, Meteora and Orca, plus Uniswap on Robinhood
Chain. A few tokens weren't on DexScreener at all, and some were bought before DexScreener indexed their pool.

What this means for data: the majors pipeline (CEX/perp candles, funding, 1d/1w features) is useless
here. These tokens trade on one DEX pool each, often from minute zero, on bonding curves that migrate
to AMMs, and most of them go to near-zero within hours. The data has to be pool-level, on-chain-fast,
and robust to the failure modes below, all of which corrupt a naive paper tracker.

## 2. Failure modes the pipeline must be built against (all observed in practice)

From an audit of a naive paper tracker and a day's sample:

1. **Quote-token contamination.** A token priced from a pool where it is the *quote* side (GMEB
   priced $2,306 vs $18.40 on its own market; PUMP recorded 3,487× too high). Rule: price a token only
   from pools where it is the **base** token, and cross-check.
2. **Missing liquidity read as $0** → false "rug" flags that froze 58% of paper rows. Rule: missing is
   `NULL`, never `0`; rug detection needs a *measured* liquidity drop.
3. **No sanity gate** → a ratcheting `max_price` up to 6.4M×. Rule: reject a print that moves >N× vs
   the last good price unless volume/swaps on-chain confirm it.
4. **Stale prices left "open" for days** after an API 404. Rule: every price carries `source_ts`;
   stale beyond the token's freshness budget = unknown, not last value.
5. **Indexer lag.** DexScreener misses brand-new pools (2 buys happened before it indexed them) and
   some tokens entirely (3). Rule: the first minutes come from the chain, not an indexer.
6. **Late entries.** Paper entries were ~45 min late by construction. Rule: record the exact
   on-chain time of the first swap and of the call, separately from ingest time.

## 3. Coverage

- **Watchlist, not firehose (v1):** every token the call feed's groups *call* or its bot *buys*
  (`deploy/call-feed/`), plus anything added by hand. Expect hundreds of tokens a day from a few busy groups. (A launchpad firehose — every pump.fun launch — is tens of thousands/day;
  see §9.)
- **Chains v1:** Solana (pump.fun curve, PumpSwap, Raydium AMM/CPMM, Meteora DLMM/DBC, Orca) and
  Robinhood Chain (Uniswap). ETH/Base/BSC are resolved when a call names them.
- **Life of a token in the lane:** tracked densely while active; demoted to hourly after 24 h with no
  volume; retired when liquidity stays under $500 for 24 h or 14 days pass, whichever is first.

## 4. Data to collect

| Data | Why | Primary source (free first) | Cross-check |
|---|---|---|---|
| **Pools per token**, which side is base, launchpad, migration events | Right price; bonding curve → AMM migration changes everything | DexScreener tokens API (30 tokens/call), GeckoTerminal | On-chain pool account / Uniswap factory events |
| **Price + 1m candles (OHLCV)** | Everything else is built on this | GeckoTerminal pool OHLCV (1m, free, both chains) | Jupiter Price API v3 (keyless lite, or the shared key); on-chain swaps |
| **Swaps / flow**: buys vs sells, unique buyers, buy/sell volume per 1m | Momentum and exhaustion on a timescale of minutes | On-chain via RPC: Solana `logsSubscribe` on the pool (Chainstack Solana endpoint), Robinhood `Swap` events via its RPC | DexScreener `txns` counts (m5/h1) |
| **Liquidity & reserves**, LP burned/locked | Can you exit? Rug risk | Pool reserves on-chain | DexScreener/GeckoTerminal liquidity |
| **Token safety**: mint & freeze authority (Solana), transfer tax / honeypot (EVM), owner privileges | Filters out untradeable tokens | RPC account reads; EVM simulation of a sell (`eth_call`) | — |
| **Holders**: top-10 share, dev wallet share, holder count growth, launch snipers/bundles | Concentration = dump risk | RPC `getTokenLargestAccounts` (Solana), Blockscout (Robinhood) | Paid indexers if needed (§7) |
| **Calls**: which group/caller, when, how many callers | Caller quality was persistent in the audit (ρ=+0.86), useful as an exclusion filter | the call feed | — |

Everything lands with `source`, `source_ts` (when the source says it happened) and `ingest_ts`.

## 5. Accuracy rules (the point of the lane)

- Price = median of the sources that agree within 5%; if only one source, flag `single_source`.
- A token is priced only as **base** in its own pool(s); the deepest pool wins; pool switches are logged.
- Reject any print > 10× or < 0.1× the last good print within 1 minute unless on-chain swaps confirm.
- Freshness budget by age: < 1 h old → price ≤ 15 s old; < 24 h → ≤ 60 s; older → ≤ 5 min. Outside
  budget = `stale` (shown and graded as unknown), never silently carried forward.
- Missing liquidity is `NULL`. "Rugged" requires two consecutive on-chain reads of liquidity < $500 or
  < 5% of the peak, not one missing field.
- A daily **reconciliation job** re-derives the day's candles for a random 20 tokens from raw on-chain
  swaps and reports the error vs what we stored. That number goes on the Health tab.

## 6. Storage (new tables, same TimescaleDB)

- `micro_tokens` (chain, address, symbol, launchpad, created_at, first_swap_at, status, retired_at)
- `micro_pools` (chain, pool, token, quote, dex, is_base, created_at, migrated_from)
- `micro_candles_1m` hypertable (chain, token, ts, o/h/l/c, volume_usd, buys, sells, unique_buyers,
  source, n_sources) — compressed after 3 days
- `micro_snapshots` hypertable (liquidity_usd, mcap, holders, top10_pct, dev_pct, mint/freeze auth,
  tax, honeypot) at a cadence set by token age
- `micro_calls` (token, group, caller, called_at, from the call feed)

Signals reuse `signals` (scope = `chain:address`, tf ∈ 1m/5m/15m/1h, source = `micro_*`), graded by
the same grader against `micro_candles_1m`, so the scorecard covers the lane with no new UI logic.

## 7. Vendors

Free, verified 2026-09-29: DexScreener (tokens API; chain id `robinhood` works), GeckoTerminal
(224 networks incl. `solana` and `robinhood`; 1m OHLCV returned 200), Jupiter Price API v3 keyless
(`lite-api.jup.ag/price/v3`, returned 200), public/Chainstack Solana RPC, Robinhood Chain RPC +
Blockscout.
Paid, optional: a **Jupiter Developer key** (100 req/10 s; if it is shared with live trading, Albatross stays
≤ 20 req/10 s and yields) and a **Solana RPC** such as Chainstack (check the plan's RPS and whether it allows
websockets). **Birdeye** (the Lite plan was $39/mo for 2.5M compute units a month at 15 req/s in 2026):
`BIRDEYE_API_KEY`; `defi/token_security` answered 200 on 2026-09-29 (it was paid-only on the
free tier), so it is the M2 source for mint/freeze authority, creator and holder concentration, with
price, OHLCV and holders in the same API. Units are metered per endpoint: give Albatross a daily unit
budget in code (2.5M/month ≈ 80k/day) and stay ≤ 10 req/s.
Paid, only if the free stack fails the §5 reconciliation: Helius (Solana webhooks/enhanced txs),
Bitquery or Moralis (multi-chain). Prices not checked yet.

## 8. Build order

- **M1 Prices you can trust:** watchlist from the call feed; pools + base-side resolution; 1m candles
  from GeckoTerminal + Jupiter cross-check; sanity/staleness rules; `Micro` dashboard page listing
  tokens with price, liquidity, age, source agreement. *Done when:* the reconciliation error is
  measured and on the Health tab.
- **M2 On-chain speed & safety:** swap subscriptions for tokens < 1 h old (first-minute coverage that
  indexers miss), liquidity/LP, mint/freeze/tax/honeypot, holder concentration.
- **M3 Micro analyst + grading:** an LLM analyst over flow + safety + call data on 1m/5m/15m/1h,
  evidence-checked like the other analysts, graded on `micro_candles_1m`; scorecard by caller/group.
- **M4 Feedback to a bot (optional):** e.g. a read-only "Albatross says" line or a veto on tokens failing
  safety, read by the bot. Nothing in Albatross ever trades.

## 9. Open questions

1. Watchlist only (what the bot sees) or also a launchpad firehose (every new pump.fun / Robinhood
   Uniswap pool)? The firehose finds tokens before the calls do, but is 100×+ the volume.
2. Chains: Solana + Robinhood now; add ETH/Base/BSC when the bot trades there?
3. Should Albatross ever feed back into a trading bot (M4), or stay read-only research?
4. Retention: keep 1m candles for retired tokens forever (research) or 30 days?

---

## 10. M1 as built (2026-09-29)

Code: `albatross/micro/` (`rules.py` pure accuracy rules, `pipeline.py` jobs + tier/rate arithmetic, `reconcile.py`),
clients `albatross/venues/{dexscreener,geckoterminal,jupiter}.py`, API `albatross/api/micro.py`, UI
`static/micro.{js,css}` (Micro tab + a block on Health), migration `008_micro_prices.sql`, tests
`tests/test_micro_rules.py`.

**How a price is made.** `micro_pools` (every minute, per tier) lists every pool of each token via DexScreener
`/token-pairs`, keeps the ones where the token is the **base** side, and prices from the deepest (a bonding curve,
whose liquidity DexScreener doesn't report, beats a pool under $500; another pool must be 25% deeper to take
over). Chain-less calls (`chain='evm'`) are resolved by asking DexScreener on robinhood, base, bsc, ethereum.
One token = one `micro_tokens` row, keyed by the chain it trades on (migration 013, `micro_fold_tokens()`): a call's
chain is only a hint (none, or any chain word in the message), so an `evm` row, an 0x address hinted `solana`, or a
named EVM chain where discovery found no pool folds into the row where a bot trade or a pricing pool puts the token;
its calls move with it. The ingest endpoint folds after every batch, the pool job after every run.
`micro_snapshots` (every 10 s, per tier) takes Jupiter (Solana), DexScreener (our pool, base side re-checked on
every quote) and our latest GeckoTerminal candle; the price is the median of the quotes that are inside the
freshness budget and agree within 5%. One source = `single_source`; no two agreeing = `disagree` (our own
pool's quote stands, unless the sources are more than 1.5× apart: then no price); a >10×/<0.1× move needs two
sources or 60 s of persistence, else `rejected_print`. The API returns `price_usd: null` once a price is past its
budget (`last_price_usd` keeps the old value for display). GeckoTerminal 1m candles are fetched with
`token=<address>` (priced in the token whatever the pool's orientation) and flagged the same way.

**Verified limits (2026-09-29).** DexScreener 300 req/min on the three endpoints, 30 addresses per call, CDN
max-age 30 s (OpenAPI spec). GeckoTerminal keyless **10 calls/min per IP**, data refreshed every 60 s, 1000 candles
per call. Jupiter: key plan 100 per 10 s, we cap at 15 per 10 s and yield when the window is half used;
keyless lite-api (being phased out) for local tests. Arithmetic per tier in `pipeline.py`'s docstring.

**Measured on a live run** (throwaway DB, 479 production `micro_tokens` rows, 2026-09-29 20:49–21:35 UTC, keyless
Jupiter, GeckoTerminal shared with another job on the same IP):

| | |
|---|---|
| Chain-less calls resolved | 91 of 96: robinhood 54, bsc 26, ethereum 8, base 3 (5 not on DexScreener) |
| Tokens with a base-side pool | 401 of 456 live: solana 293/333, robinhood 72/78, bsc 25/26, ethereum 8/11, base 3/3 |
| Pools seen | 1,847; 249 quote-side ones refused, one of them deeper than any base-side pool (the GMEB pattern) |
| Pool switches | 401 initial, 1 live migration caught (`convict`, pump.fun curve → PumpSwap $13.5k, 21:17 UTC) |
| Price with ≥ 2 agreeing sources | 262 of 333 Solana tokens; EVM chains mostly single-source (DexScreener) |
| Sources disagree (> 5%) | 17, mostly Raydium pools where Jupiter's last swap sits 5–9% under the pool price |
| Candles | 60,013 1m candles for 134 tokens (GeckoTerminal budget halved by the other job); of the 886 minutes since quotes began, 51% corroborated by another source |
| Rejected prints | 0 in the run (and so no false rejections) |

Reconciliation, two runs (21:20 scheduled, 21:28 manual), 772 comparisons over 35 tokens:

| Comparison | n | Median abs error | p90 | Within 5% | Inside the candle |
|---|---|---|---|---|---|
| Jupiter vs GeckoTerminal 1m candle (Solana) | 206 | 0.11% | 3.8% | 96% | 92% |
| DexScreener vs GeckoTerminal candle, Solana | 211 | 0.15% | 3.8% | 94% | 94% |
| DexScreener vs GeckoTerminal candle, Robinhood + BSC | 118 | 1.0% | 7.5% | 84% | 82% |
| Jupiter vs DexScreener, same poll | 237 | 0.08% | 3.8% | 95% | – |

The EVM chains' larger error vs the close is mostly timing, not bias: signed means per Robinhood token were
+0.7%, +2.3% and −1.0%, and those pools moved ±20% within minutes; "inside the candle" is the fairer measure.

**Known gaps.** No on-chain source yet, so: hot Robinhood/BSC tokens are stale between DexScreener refreshes;
the first minutes before an indexer sees a pool are missing; buys/sells/unique buyers stay NULL; "rugged" and
the spec's candle re-derivation from raw swaps wait for M2. Market cap is often NULL on pump.fun curves
(DexScreener reports none for many). A pool quoted in another micro-cap has no USD liquidity, so it can't win on
depth. A retired token is not revived by a new call. Quote history (`micro_snapshots`) measured ~210 rows/min,
~300k/day, at 456 tokens; compressed after 3 days, no retention policy (§9.4).

