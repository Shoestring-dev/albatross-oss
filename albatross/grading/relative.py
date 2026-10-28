"""Relative grading: does a source pick coins that beat the others, whatever the market did?

Direction is close to unpredictable on our data (docs/RESEARCH-EDGE.md), so grading every call only on
"did price go my way" can hide real skill at telling strong coins from weak ones. For each source,
timeframe and run (all calls sharing an as_of), the run's market move is the mean raw return of every coin
it called (bias 0 included); a directional call's relative result is its side times (its coin's return
minus that market move). A source with relative edge makes money on a book that is long its bullish calls
and short its bearish ones in equal size, even in a market it can't time.

Runs overlap (a 4h call made every hour shares most of its window with the next), so the t statistic
discounts n by the overlap: effective runs = runs x run spacing / horizon.
"""

import math
from collections import defaultdict

from albatross import db
from albatross.features.engine import HORIZON_BARS, TFS

MIN_EFFECTIVE = 20          # effective independent runs before a verdict


async def by_source_tf() -> list[dict]:
    pool = await db.pool()
    # raw return: outcomes.return_pct is signed to the call's side, and bias-0 calls keep the raw move
    rows = await pool.fetch(
        """SELECT s.source, s.tf, s.as_of, sign(s.bias)::int AS side,
                  o.return_pct * (CASE WHEN s.bias < 0 THEN -1 ELSE 1 END) AS raw
           FROM signals s JOIN outcomes o ON o.signal_id = s.id
           WHERE o.return_pct IS NOT NULL AND s.scope NOT LIKE 'sector:%' AND s.source <> 'oracle'""")
    runs: dict[tuple, list] = defaultdict(list)
    for r in rows:
        runs[(r["source"], r["tf"], r["as_of"])].append((r["side"], r["raw"]))
    per: dict[tuple, list] = defaultdict(list)
    for (src, tf, as_of), calls in runs.items():
        if len(calls) < 5 or not any(side for side, _ in calls):
            continue                                   # too few coins to have a market, or no view
        mkt = sum(raw for _, raw in calls) / len(calls)
        rel = [side * (raw - mkt) for side, raw in calls if side]
        per[(src, tf)].append((as_of, sum(rel) / len(rel), sum(x > 0 for x in rel) / len(rel), len(rel)))
    out = []
    for (src, tf), items in per.items():
        items.sort()
        n = len(items)
        vals = [x[1] for x in items]
        mean = sum(vals) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        horizon = TFS[tf][2] * HORIZON_BARS
        gaps = sorted((b[0] - a[0]) for a, b in zip(items, items[1:])) if n > 1 else []
        spacing = gaps[len(gaps) // 2] if gaps else horizon
        eff = n * min(1.0, spacing / horizon) if horizon.total_seconds() else n
        t = mean / sd * math.sqrt(eff) if sd > 0 and eff > 1 else 0.0
        status = ("early" if eff < MIN_EFFECTIVE else "proven" if t >= 2 else "worse" if t <= -2 else "none")
        out.append({"source": src, "tf": tf, "runs": n, "effective_runs": round(eff, 1),
                    "calls": sum(x[3] for x in items), "rel_return_pct": round(mean, 3),
                    "rel_hit_rate": round(sum(x[2] * x[3] for x in items) / max(1, sum(x[3] for x in items)), 3),
                    "t": round(t, 1), "status": status,
                    "words": {"early": "too early to tell", "proven": "picks coins that beat the rest",
                              "worse": "picks coins that lag the rest", "none": "picks coins no better than the rest"}[status]})
    return out
