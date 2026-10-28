# Where the edge is (research, 2026-09-30)

Does Albatross actually find alpha? This is the evidence, from our own data, and what the product does with it.
The research scripts were one-off; the live code is `albatross/strategy/factor_rank.py` and `/api/intel`.

## 1. The signals we already publish

Graded against the better of always-long / always-short on the same windows (`grading/scorecard.py`,
overlapping windows discounted to n / 6):

| Source | History | Result |
|---|---|---|
| Quant model, 5-month walk-forward replay | ~10,000 graded calls per timeframe | **No edge on any timeframe**; worse than naive on 5m, 15m, 4h and 1w (e.g. 4h: 49.2% vs 54.6%) |
| Quant model, live | 1 day | Worse than naive, but one market-wide day is one sample |
| LTF analyst, Ensemble, Strategist | ~1 day | Too early to tell |

Cross-sectionally, the quant model's 1d score does not rank coins either: rank IC 0.006 (t 0.2) over
April-September. (A first pass showed IC 0.11: that was a look-ahead in the research script, which put a
signal computed from day d's close on day d-1. `signals.as_of` is the candle's **open** time; the grader
itself is correct, it starts the window at `as_of + bar`.)

The LLM layers read the quant features, so on this evidence they start from nothing predictive.

## 2. What does work on our universe

Daily Hyperliquid candles 2022-2026, liquid coins only (30-day median dollar volume > $5M; ~23-33 coins
a day recently). Rank IC = Spearman correlation between a factor and the next period's return across
coins, sampled on non-overlapping windows.

| Factor | 1-day IC (2022-26) | 7-day IC (2025-26) | Note |
|---|---|---|---|
| Low 30-day volatility | 0.083 (t 6.2) | 0.157 (t 3.9) | calmer coins outperform; the spread is small, mostly "avoid the wildest" |
| Near the 20-day high | 0.025 (t 2.6) | 0.086 (t 2.8) | Q5-Q1 spread significant (t 3.4 at 1d) |
| Volume shock (vs 30-day norm) | 0.006 | 0.077 (t 3.1) | Q5-Q1 spread t 2.4 |
| Momentum 7/30/90d, reversal 1/3d, distance from MA50/200 | ~0 | ~0 | nothing |

**Composite** (mean z-score of the three), top fifth minus bottom fifth, after Hyperliquid taker fees and
5 bp slippage per leg, turnover-adjusted:

| Period | Hold | IC | Net spread per hold | t |
|---|---|---|---|---|
| 2025-01 .. 2026-09 | 7 days | 0.149 (t 5.3) | +1.9% | 2.1 |
| 2025-01 .. 2026-09 | 3 days | 0.085 (t 4.3) | +0.7% | 1.7 |
| 2024 | 7 days | 0.102 (t 1.7) | +2.9% | 1.5 |
| 2022-23 | | too few liquid coins on Hyperliquid to test | | |

Caveats: the universe is today's coins (survivorship flatters it); funding paid or received on the short
leg is not netted yet (§4); the long leg alone beat the universe by ~0.7% a week, most of the spread is the
short leg. Treat it as a ranking to lean on, not a finished strategy.

## 3. Market timing

Next-7-day BTC and equal-weight market returns by quintile of each series, weekly samples 2024-10..2026-09:
Fear & Greed, BTC ETF 5-day flows, stablecoin supply 30-day change, BTC vs its 200-day average, BTC 30-day
momentum, DXY and 10-year 20-day changes, VIX. **None is monotonic or significant** (rank correlations
-0.06..+0.17 on ~100 weeks). They are context for the Today page, not signals.

## 3b. The MVRV Z-score (a cycle gauge), added 2026-09-30

BTC forward returns by MVRV Z zone, daily samples 2011-2026 (Coin Metrics market cap, MVRV and price; Z with
the expanding deviation, first 180 days skipped):

| Z | next 90 days | next 180 days | next 365 days |
|---|---|---|---|
| below 0 | median +15%, up 68% | median +60%, up 83% | median +123%, up 99% |
| 0 to 1 | +5%, 54% | +27%, 61% | +111%, 83% |
| 1 to 2 | +21%, 66% | +41%, 66% | +124%, 68% |
| 2 to 3.5 | +1%, 52% | +10%, 56% | -5%, 48% |
| 3.5 to 6 | +64%, 69% | +30%, 70% | +22%, 62% |
| 6 and up | -29%, 31% | -27%, 15% | -36%, 26% |

Read: the extremes carried information over months (below 0 was a buying zone in every cycle; 6+ preceded
every drawdown); the middle said little, and BTC's secular rise flatters every row. About four cycles and
heavily overlapping samples: history, not a tested edge. It is shown on Today and fed to the LLM layers as a
months-scale gauge, never as a signal for days.

## 4. Positioning (funding) and what is still to test

Hyperliquid hourly funding, 2025-01..2026-09, 26 coins (~18 liquid with funding on a given day):

| Factor | 1-day IC | 7-day IC | Q5-Q1 net per week |
|---|---|---|---|
| Crowding (minus funding, 3d / 7d) | 0.016 / 0.017 | 0.023 / 0.003 | about -1.4% (t -1.4): no edge |
| Funding rising | 0.006 | 0.004 | no edge |
| Composite (the three price factors), this narrower universe | 0.046 (t 3.4) | 0.121 (t 3.3) | +1.1% net of fees AND funding (t 0.9) |
| Composite + crowding | 0.045 | 0.115 | -0.1%: crowding makes it worse |

Crowded funding does not predict underperformance here, and funding carry on the long/short book is about
+0.05% a week, so it neither erodes nor adds to the ranking. The composite's rank IC survives on the
narrower universe; its dollar spread is noisy with ~18 names (quintiles of 3-4 coins).

Still to test: open-interest history (Coinalyze daily) for OI surges; the micro-cap callers once the DEX
ledger (since 2026-09-29) has a few days of 24h outcomes.

## 5. What the product does with this

- `factor_rank` job (00:30 UTC daily) ranks the liquid coins; `live` rows are the honest track record,
  backfilled rows are labelled history.
- `albatross/strategy/evidence.py` is the single place the LLM layers get "what's measured": the analysts'
  prompts no longer carry the old claim that trend-following had an edge on 1w (the replay said worse);
  the Strategist sees each coin's factor rank and every source's record, and **code rejects an idea that
  goes long a coin in the weakest two fifths or short one in the strongest two**; the critic checks it too;
  the 4-hourly report opens with the evidence-backed view.
- Relative grading (`albatross/grading/relative.py`): for every source, do its bullish coins beat its
  bearish ones within the same run, whatever the market did? This is the test that matters when direction
  is unpredictable; on 2026-09-30 the quant model shows no coin-picking edge either and the LLM sources are
  too new to judge.
- `/api/intel`: each source's direction and coin-picking record in plain words, today's strongest and
  weakest coins with reasons, the ranking's backtest and live record, and what is unusual now.
