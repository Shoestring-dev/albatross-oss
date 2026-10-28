"""Fair baselines for direction calls (docs/PLAN-LEARNING-LOOP.md section 6).

The scorecard's "naive" caller is the better of always-long and always-short, picked after the fact: a ceiling no
caller can reach in advance, which makes every source look worse than it is. The fair baseline is what random calls
with the source's own long/short mix would have hit on the same windows:

    matched = long_share * rose + (1 - long_share) * (1 - rose)

so skill = hit - matched measures timing and selection, separately from a standing bias (on 2026-10-01 the agents
called long 75-98% of the time while prices rose in 43-50% of those windows). Always-long is reported too: the
simplest baseline anyone could actually run. Overlapping windows count as 1/HORIZON_BARS of a sample, and the 95% band
is the scorecard's (scorecard.vs_naive), so the numbers compare with the Track record's.
"""

import math

from albatross import db
from albatross.features.engine import HORIZON_BARS

WINDOW_DAYS = 30


def _band(hit: float, n: int) -> float:
    q = (hit * n + 1) / (n + 2)
    return 1.96 * math.sqrt(q * (1 - q) / max(1.0, n / HORIZON_BARS))


def fair(n: int, hit: float, long_share: float, rose: float) -> dict:
    """One source and timeframe: n graded calls, their hit rate, the share called long, the share of windows where
    price rose."""
    matched = long_share * rose + (1 - long_share) * (1 - rose)
    return {"n": n, "n_eff": n / HORIZON_BARS, "hit": hit, "long_share": long_share, "matched": matched,
            "always_long": rose, "hindsight": max(rose, 1 - rose), "skill": hit - matched, "band": _band(hit, n)}


def pooled(rows: list[dict]) -> dict | None:
    """A source over all its timeframes: n-weighted hit and baseline, the band from the pooled count."""
    n = sum(r["n"] for r in rows)
    if not n:
        return None
    hit = sum(r["hit"] * r["n"] for r in rows) / n
    matched = sum(r["matched"] * r["n"] for r in rows) / n
    return {"n": n, "n_eff": n / HORIZON_BARS, "hit": hit, "matched": matched, "skill": hit - matched,
            "band": _band(hit, n), "tfs": sorted(rows, key=lambda r: r["tf"])}


async def by_source(days: int = WINDOW_DAYS) -> dict[str, list[dict]]:
    """Live direction calls on assets (sector roll-ups are graded against their own index): source -> per-timeframe."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT s.source, s.tf, count(*) AS n, avg(o.hit::int)::float AS hit,
                  avg((s.bias > 0)::int)::float AS long_share,
                  avg(((o.return_pct > 0) = (s.bias > 0))::int)::float AS rose
           FROM signals s JOIN outcomes o ON o.signal_id = s.id
           WHERE o.hit IS NOT NULL AND s.bias <> 0 AND s.scope NOT LIKE 'sector:%'
             AND s.as_of > now() - make_interval(days => $1)
           GROUP BY 1, 2""", days)
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["source"], []).append({"tf": r["tf"], **fair(r["n"], r["hit"], r["long_share"], r["rose"])})
    return out
