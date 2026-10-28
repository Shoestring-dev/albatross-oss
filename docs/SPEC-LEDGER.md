# Trade ideas, the call ledger & backtesting — spec v0.1

**Added 2026-09-29.** The direction: *Albatross is the source of truth that will eventually make and
manage positions.* Before anything trades, every trading call from every source is logged
immutably and backtested for accuracy. Status: **spec; the first input (the call feed) is live.**

## 1. Principle

One ledger for every call, whoever made it, graded one way, net of realistic costs, with no way to
edit history. Execution (later, user-defined) only ever acts on ideas that came through this ledger
and only for sources whose graded record justifies it.

## 2. Sources of calls

| Source | Examples | Status |
|---|---|---|
| Albatross agents | ltf/htf/flow analysts (directional bias per asset/tf) | live in `signals`, graded |
| Strategist / Oracle | merged bias → concrete trade ideas (entry, stop, targets) | to build |
| Telegram callers | every contract address called in the call feed's groups | **feed live** (`micro_calls`) |
| Bot trades (call feed) | every position, with on-chain tx signatures | **feed live** (`micro_trades`) |
| Rules / screeners | e.g. "buy every call from group X with liquidity > $10k within 2 min" | to build |
| Manual | the owner logs a call from the dashboard | to build |

## 3. The ledger (`trade_calls`, append-only)

Each row: `id`, `logged_at` (Albatross server time, the only clock trusted), `source_kind`,
`source_id` (agent / caller / group / trader pseudonym / rule), `scope` (symbol or `chain:address`),
`market` (perp | spot | dex), `direction`, `entry` (market or limit; **reference price taken from
Albatross's own price feed at `logged_at`, never the caller's claim**), `stop`, `targets[]`,
`horizon`, `size_hint`, `thesis`, `evidence`, and `prev_hash` + `hash` (SHA-256 over the row and the
previous hash).

- **Immutable:** no UPDATE/DELETE (enforced with a trigger); a change is a new row pointing at its
  parent. The hash chain makes after-the-fact edits detectable.
- **No look-ahead by construction:** a call is logged before its outcome exists; the grader starts at
  the first bar after `logged_at` (the same rule the Flow analyst already follows).
- Group calls become `trade_calls` automatically (long, market entry at our price when the call
  arrived) so every caller gets a real track record.

## 4. Backtester

Two modes, one engine:

1. **Forward grading** of logged calls as their horizons pass (what the grader does today, extended).
2. **Rule replay:** evaluate a rule over the historical ledger + candles (e.g. "follow caller X only
   when liquidity > $20k"), walk-forward with a held-out period.

Per call it simulates fills and exits on stored candles (1m for micro, the call's timeframe for majors):
- **Costs are not optional.** Majors: taker fee + spread. DEX: venue fee + price impact from pool
  liquidity (constant-product estimate for the size) + priority/gas. The model is **calibrated
  against a bot's real fills** (tx signatures in `micro_trades`), which is why those are kept.
- Exits evaluated for several policies side by side: fixed horizons (micro: 5m/1h/6h/24h; majors:
  6 bars), stop/target first-touch on 1m candles, and the bot's own ladder.
- Output per call: net return, R multiple, MFE/MAE, max multiple, time-to-rug, fill feasibility
  (could the size have filled at all?).

**v1 built 2026-09-29 (majors, forward grading only):** `albatross/backtest/` (`sim.py` pure rules,
`ideas.py` the `grade_ideas` job, every 5 min) → `trade_call_outcomes` (migration 007), one row per call
per policy (`stop_target`, `horizon`), recomputed while open; `regrade_ideas` rebuilds everything.
`/api/ideas/scorecard` compares against always-long and random direction on the same windows. Costs:
Hyperliquid taker fee + square-root slippage for a $10k reference size + Hyperliquid funding (only
where snapshots exist, i.e. from 2026-09-29 01:18 UTC). Not yet: spot (`ungradeable`), bot
fill calibration, revisions (`parent_id`) graded as independent calls, rule replay.

**DEX calls, built 2026-09-29:** `micro/ledger_feed.py` (job `micro_ledger`, every minute) logs every
Telegram group call (`source_kind` caller) and real-money bot entry (bot) at OUR trusted price
(`micro/rules.live_price`, the Micro tab's rule) once one exists within 10 min of the call; the rest are
counted in `micro_ledger` (migration 011) as unpriced (`late`, `missed`, `no_price`), sells skipped.
Long, no stop/targets, 24 h horizon; the bot's own fill is kept in the evidence for calibration.
`grade_ideas` grades them on `micro_candles_1m` (`sim.simulate_dex`): pool fee by DEX, constant-product
impact on both legs for a $100 position (`DEX_REF_NOTIONAL_USD`; bot buys are often smaller, and $10k
would only have measured pool depth) against half the pool's USD liquidity, a network allowance per
chain; net return at 5m/1h/6h/24h (`ret_*_pct`) and at the horizon. Each outcome row keeps its cost
inputs (size, fee, gas, entry liquidity and quote reserve, and price + pool at every exit in `exits`),
so another size is a regrade from the row alone. `evidence.call_type` (buy | mention | bot_entry) splits
the idea scorecard; DEX calls stay out of the report and Oracle prompts (`scorecard.NOT_DEX`).

## 5. Honesty rules for results

- Always shown against naive baselines on the same windows: always-long, buy-every-call, random entry.
- Confidence bands that account for overlapping windows and correlated assets.
- **Count the rules tried.** An earlier audit tested 235 slices and found none above break-even;
  with that many tries ~3 look significant by chance. Rule replays report the number of variants
  tested and hold out data the search never saw.
- A source graduates to "eligible for execution" only on out-of-sample results, net of costs.

## 6. Identifying potential trades

- **Majors:** the Strategist emits a trade idea when timeframe biases align with the regime, flow
  doesn't contradict, and the reward:risk to the invalidation level clears a threshold. Each idea
  goes into the ledger; nothing is shown as a "trade" that isn't logged.
- **Micro:** a screener over incoming calls + on-chain safety/liquidity/flow (SPEC-MICROCAPS M2)
  proposes ideas; the caller/group track record from the ledger is an input (an earlier audit found caller
  quality persistent, ρ = +0.86, but useful mainly as an exclusion filter).

## 7. Toward managing positions (later, when the owner defines execution)

Albatross holds the ledger, the positions and the risk state; an execution service (separate
process, own risk engine, keys never withdrawal-enabled, only venues legal where you are) acts on eligible
ideas. A trading bot can become one execution adapter for DEX tokens. Until then, Albatross stays
read-only toward every venue and wallet.

## 8. Build order

1. `trade_calls` + hash chain + immutability trigger; group calls and analyst calls flow in.
2. Micro price pipeline (SPEC-MICROCAPS M1) so DEX calls can be priced and graded.
3. Backtester v1: forward grading with the cost model, calibrated on a bot's real fills; Scorecard
   gains "by caller / group / trader / rule".
4. Strategist trade ideas (majors) into the ledger.
5. Rule replay with walk-forward + variant counting.

## 9. Paper books (built 2026-10-02)

What following each kind of call would have made, in dollars, so the owner can judge whether the product works
(`albatross/backtest/books.py`, job `paper_books` every 15 minutes, stored whole in `paper_reports`, migration 036;
shown at Calls › Paper books, `GET /api/books`). Nothing trades. Each book is rebuilt from the ledger, the outcomes,
the dossier history and the daily candles, so the books carry no state of their own.

- **The Deep End**, $100 a trade at the call's ledger price with the DEX cost model, one position per coin (a call
  on a coin the book still holds is skipped):
  - every Telegram call, the baseline;
  - no red flag;
  - score 85+;
  - sold at 2x;
  - FOMO buys;
  - the bot from the call feed as a reference.
- **Big coins:**
  - factor rank long/short and long-only: seven daily slices of $10,000 held a week, with fees, slippage and
    funding; long-only is shown against the same slices in every ranked coin;
  - the Strategist's kept ideas at $100 risked each.
- **Added 2026-10-02 after the first results**, counted only from Oct 3 00:00 UTC:
  - score 85+, sold at 2x (the two least-bad rules so far, together);
  - the opposite of the quant model's 1h calls, held for the grader's 6-bar window, against the same side on an
    average coin the model called that hour, so the market's drift isn't counted as skill on either side (the quant
    model's calls were wrong more often than chance: -5.4 pts ± 0.8 on 16,190 windows). The same day the quant model
    went on probation, out of the ensemble.
- **Rules set 2026-10-02.** The Deep End rules were fixed without looking at the result of any call that had a
  dossier score; 82 had finished by then. The score cut-off came from the score distribution alone. Factor rank was
  picked on its history, so only its live weeks count.
- **A dossier rule** sees only the dossier as it stood when the call was logged, and only a scored one (5+ lines
  known).
- **Counting:** a trade counts in a card's dollars once the call's 24 h are graded, in every book alike. Until then
  it is open and marked at its latest price, re-marked at least hourly by the grader.
- **Comparisons:** each book is set against what it should beat, on the same days:
  - the dossier rules against every call that had a scored dossier;
  - sold at 2x against holding the same calls (paired);
  - FOMO and the bot against every call since their first entry.
- **Verdicts** wait for a sample:
  - memecoins: 100 closed trades over 14 days;
  - the Strategist: 60 ideas over 14 days;
  - factor rank: 6 weeks.
  Intervals resample days; each card says when its first read is due.
- **The dossier report:** graded Telegram calls with a scored dossier at the call.
  - Set in advance: red flag against none, and the score's rank correlation with the 24 h return (intervals
    resample coins; first read at 500 calls with 100 a side).
  - Score bands, single flags and lines by status are exploratory.
- **Exit policies:** DEX calls gained `take_profit`, sold at the first 1m close that nets 2x after every cost, else
  at 24 h (`sim.TAKE_PROFIT_PCT`). Closes, not highs: a bot sells at market. The grader backfilled it for calls
  graded before it existed and never rewrites a resolved row.

