# The learning loop: plan v0.2 (2026-10-01)

The goal (2026-10-01): a system that critiques itself and changes over time, aiming at the most accurate intel, and
in the end intel that leads to profitable trades. After the first plan: no real money until paper is profitable, and a
system for cutting agents that don't hold their weight. v0.2 folds in that decision and a review of v0.1, which
found the gate statistically unworkable, the yardstick unfair, history tests invalid for anything with a
language model in it, and the Deep End left out (section 9).

## 1. The goal and how it is scored

The goal is profitable trades that our agents make and manage, **on paper until a strategy is proven
profitable**. Real money is off the roadmap until then, and comes back only on the owner's word.

**The north star** is the champion book's paper return after fees, slippage and funding, within a drawdown
budget. It is judged by its leading test first and its profit second (section 6). Leading indicators count only
while they predict the north star:
- calibration (Brier, log loss);
- ranking (rank correlation of scores with next-period returns);
- marginal value (does adding a source raise the champion's results?).

## 2. Where we start (evidence, 2026-10-01)

- **One measured edge, still a hypothesis.** Cross-sectional relative strength: low 30-day volatility, near the
  20-day high, volume shock.
  - Top fifth minus bottom fifth, held 7 days, made +1.9% a week after costs in the 2025-26 backtest
    (docs/RESEARCH-EDGE.md).
  - Its t of 2.1 came after trying about ten factors, so it is weaker than it looks.
  - Most of the spread is the short leg; the long leg alone beat the universe by ~0.7% a week.
  - Live tracking started 2026-09-29.
- **No source shows direction skill.** On the fair baseline (section 6), most agents are roughly random given
  their own long/short mix. The LTF analyst on 1h looks worse. The agents call long 75-98% of the time, while
  prices rose in only 43-50% of those windows.
- **Jev** (TypeSafe `jev-1.13.0`, a fixed external model we cannot train):
  - its odds ran 2-3x too high (corrected since 2026-10-01);
  - its long-or-short pick is a coin flip;
  - every confidence slice of its 2R, 6-candle candidates loses after costs.
- **Already built:** the hash-chained ledger and grading after costs; the candle simulator; source grading; the
  accuracy-weighted ensemble; the shadow-model switch; Jev's calibration and replay; the Output and Agents
  pages; pause and budget controls.
- **The Deep End ledger** grades every group call within 24 hours: the fastest feedback in the system.

## 3. What "Jev gets better" can mean

We cannot change Jev. What can learn is everything around it:
- **The question.** Ask in the shape of our edge: "over 7 days, does A beat B?"
- **The state** we send it.
- **A learner** that uses its odds as one input.
- **The action:** selection, size and exits.
- **The version:** each release is shadow-scored before use.
- **Feedback,** if TypeSafe offers it.

Jev is also subject to culling (section 7). If no Jev variant adds value over the base book by the second
checkpoint, the scout is retired.

## 4. The loop

1. **Decide.** Every output and every candidate, taken or not, is written with its inputs at decision time
   (deduplicated: each coin's state once per run, referenced by hash), the model versions and the variant id.
2. **Grade.** Outcomes after costs land in the ledger.
3. **Critique.** A post-mortem agent may cite only precomputed figures with intervals. It proposes experiments
   as schema-checked configs with bounded parameters. It cannot change production. It is graded too: do its
   proposals pass more often than random ones?
4. **Test.** Code-only variants are replayed on history, then run in paper. Anything containing a language
   model or Jev is tested forward in paper only: those models were trained on the period we would replay and
   may simply remember it.
5. **Decide at checkpoints.** The gate rules at fixed checkpoints (every 6 weeks), never nightly: a nightly
   t-test eventually passes luck. Nightly jobs only compute and display.
6. **Learn.** The learner retrains on decision records with purged walk-forward validation.

## 5. Phases

**Phase 0: foundations (1-2 weeks), culling first**
- **Fair baselines.** Grade direction calls against random calls with the agent's own long/short mix, plus
  always-long; the hindsight "better of always-long or always-short" stays only as a ceiling. Done when the
  Agents page and the culling reviews use them; the Track record and the LLM prompts follow.
- **The culling system** (section 7). Done when the first weekly review has run, its ladder moves are visible on
  the Agents page, and changes reach Telegram.
- **Decision records and an experiment registry** (variant id on every paper trade). Done when every Jev
  candidate and Strategist idea carries both.
- **The checkpoint gate** with day-clustered intervals and pre-defined regimes (section 6). Done when it
  recomputes from scratch and passes tests with planted good and bad strategies.

**Phase 1: a simulated exchange and the base book (weeks 1-3)**
- **A paper venue with management**, so "profitable on paper" means what the agents would really do: orders at
  realistic prices (spread plus depth-based slippage), rebalances, stops, funding and costs.
- **The factor-rank book**, long top fifth and short bottom fifth, in seven daily-staggered tranches (each
  rebalanced weekly). Judged first by its weekly rank correlation, then by profit.
- **The long-only version** (top fifth against the equal-weight universe) runs from day one, so we know the part
  of any profit an account that can't short could actually take.
- **Status 2026-10-02:** both factor books run as paper books (docs/SPEC-LEDGER.md §9). They hold seven daily slices
  marked on daily closes with fees, slippage and funding: no orders, stops or intraday management yet. The weekly
  rank-correlation test still has to be built.

**Phase 2: Jev as a challenger, forward only (weeks 3-9)**
- V1: skip the book's picks Jev rates worst for their side (asked at the book's 7-day horizon).
- V2: "over 7 days, does A beat B?" for pairs within the universe, turned into a ranking. It must beat factor
  rank's own ordering.
- V3: Jev's "will it move" for sizing. It must beat a simple recent-volatility estimate.
- V4: new Jev versions shadow-scored on a sample.
- Each runs in paper beside the base book and is judged at the checkpoints.

**Phase 3: the learner (from week 4)**
- First, a code-only version pretrained on the years of price, volume and funding history, where replays are
  valid.
- Then the same model with Jev's and the LLMs' outputs added, judged forward only.
- Few pre-registered inputs, purged walk-forward, every candidate graded (no selection bias). It must beat
  factor rank alone.

**Phase 4: self-critique (from week 2)**
- The daily post-mortem described in section 4.
- A weekly brain review for the owner: what changed, what was promoted, retired or culled, and why.

**Phase DE: the Deep End track (from week 1)**
- Grade the dossier score, the callers, and which evidence (holders, X, culture fit, security) came before the
  winners, on the DEX ledger's 24-hour outcomes.
- Paper entries priced with realistic DEX slippage, priority fees and pool depth, never the mid price.
- It runs the same gate, at its own faster cadence, because samples arrive daily.
- **Status 2026-10-02:** the dossier report (red flag vs none, score vs return) and six memecoin paper books are
  live at Calls › Paper books (docs/SPEC-LEDGER.md §9). The callers and the per-line evidence are shown, not yet
  judged.

## 6. The gate (pre-registered; changed only with the owner)

- **Checkpoints:** every 6 weeks from a variant's start. Nothing is promoted or retired between checkpoints,
  except demotions by kill rules.
- **Leading test, for ranking strategies:** the weekly rank correlation between the strategy's scores and
  next-7-day returns across the universe. It must have mean > 0 with t >= 2, using intervals clustered by
  week. At backtest strength that takes about 13-25 weeks, so this is a months-long process by design.
- **Confirmation:** paper profit after fees, slippage and funding, with day-clustered intervals. It must be
  positive and must beat always-long, random picks with the same mix, and the current champion (as a paired
  difference).
- **Regimes, defined in advance:** BTC's 30-day trend (up, flat, down) and realised volatility (top third or
  not). It must be positive in each regime it has met with enough data.
- **Replication:** pass at two consecutive checkpoints, the second on fresh data.
- **Kill rules (any time):** a drawdown past budget, or slippage above 2x the model, sends it back to paper review.
- **Many variants:** at most 8 at once. When more than 5 run, the bar rises to t >= 2.5.

## 7. The culling system (agents that don't hold their weight)

- **Weekly review**, Thursdays 01:00 UTC. A review at least 6 days after the previous one counts toward the
  ladder; earlier reviews are recorded but move nothing.
- **Evidence over the last 30 days:**
  - *Direction agents:* skill = hit rate minus the hit rate of random calls with the agent's own long/short mix,
    pooled over its timeframes. Overlapping windows count as one sixth of a sample, and the 95% band matches the
    scorecard's.
  - *Trade agents:* the Strategist's ideas, and the Jev scout's top-decile candidates. Mean R after costs per
    day, with intervals across days.
  - *Critics:* veto quality once 60 ideas are graded.
  - *Feeders, writers and the Deep End:* unreviewed until ablation or grading exists, but their cost is shown.
- **Verdicts:**
  - *earns its place:* the band is above 0;
  - *no skill:* the band includes 0;
  - *harmful:* the band is below 0;
  - *too early:* fewer than 30 effective samples (7 days for trade agents).
- **The ladder (automatic, logged, reversible):**
  - one weak review: **watch**;
  - two in a row: **probation** (out of the ensemble, half budget);
  - four in a row: **paused** (out of the ensemble, a tenth of the budget, the chart and flow analysts run on a
    10% sample so they can earn their way back);
  - **retired** only when the owner confirms;
  - any review that "earns its place" returns the agent to active.
- **Exempt:** the ensemble (the aggregator), factor rank (the base edge, judged by its own record), grading,
  monitoring and safety jobs.
- **Visible:** the Agents page shows each agent's rung, evidence and next review, and the owner can retire or
  reinstate from there. Each weekly review that moves anyone sends one Telegram message.
- **Cuts are experiments:** if the ensemble's grade worsens after a cut, the agent is reinstated at the next
  checkpoint.
- **First cuts, 2026-10-02, for the agents shown not to help:**
  - The LTF and Flow analysts were put on **paused** early, without waiting for four reviews. Each was "no skill" on
    the first review and together they cost ~$4.85/day. Now each reads 10% of coins (~$0.50/day together) and stays
    graded, so either can earn its way back.
  - The Jev analyst was **retired**: harmful on its one question (direction on 1h/4h/1d). Paused would have run it
    only until a 5-cent cap each morning, a biased sample.
  - The Jev scout keeps only 1h/4h/1d candidates (~$0.20/day instead of ~$2.10); 5m and 15m are context only
    (docs/MODELS.md).
  - Later the same day the quant model went on **probation** early: harmful, -5.4 pts ± 0.8
    on 16,190 windows, and at 5m and 15m it was the ensemble's only voice. With the LTF analyst paused, the ensemble
    now writes nothing at 5m and 15m. A paper book tests the opposite of its 1h calls from Oct 3.

## 8. Guardrails

- Agents propose; code and evidence decide. No agent edits production code, prompts or rules.
- Every change is logged and reversible.
- Execution and risk are deterministic code. Models advise.
- No look-ahead: decision records at decision time, as-of replays, and no history replays of models trained on
  that history.
- The critic's inputs (headlines, X posts) can carry planted instructions. It is fenced by schema-bounded
  proposals and has no write path to production.
- Each experiment has its own budget. LLM variants run on a sample.
- The gate job is tested with planted strategies and recomputed independently each week.

## 9. What changed from v0.1 (the review)

1. A weekly profit test on 8 rebalances could not recognise a real edge (about 1 chance in 10). The ranking now
   leads, and the timeline is months.
2. A nightly t-test would manufacture winners by luck. Decisions now happen at checkpoints.
3. The hindsight "naive" baseline made every agent look worse than it is. Grading now uses fair baselines.
4. History replays are invalid for LLM and Jev variants. Those are tested forward only.
5. Management sat in the real-money phase. It moved to the simulated exchange in Phase 1.
6. The base book's profit may be untradable where shorting perps isn't allowed. A long-only variant now runs alongside.
7. The Deep End had no loop. It now has its own track.
8. The learner would have leaked and overfit. It now uses purged walk-forward, all candidates and a code-only
   pretrain.
9. The critic would have told stories. Its proposals are now bounded, and it is graded.
10. Jev had no stopping rule. It now faces the culling rules.
11. Challengers lacked their own yardsticks. Each now has one.
12. Same-day trades were counted as independent. Intervals are now clustered by day.
13. Regimes are now defined in advance; each experiment has its own budget; the gate is tested; each phase has a
    "done when"; the decision store is deduplicated.

## 10. Decisions still open

1. **TypeSafe:** ask whether Jev accepts outcome feedback or fine-tuning? (a draft is ready on request)
2. **The culling thresholds** in section 7 are pre-registered; change them only before the first counted review.
3. **Real money:** not on the roadmap until a strategy passes two checkpoints on paper.
