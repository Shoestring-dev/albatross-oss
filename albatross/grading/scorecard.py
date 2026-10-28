"""Graded-call statistics shared by the dashboard scorecard and the report writer."""

import math

from albatross import db
from albatross.features.engine import HORIZON_BARS

# 1 when price rose over the call's window (return_pct is signed to the call's side), so its mean is
# what an always-long caller would have scored on the same windows: the benchmark a call has to beat.
ROSE = "((o.return_pct > 0) = (s.bias > 0))::int"


MIN_N = 20      # graded calls a hit rate needs before the dashboard colours it


def vs_naive(hit: float, long_rate: float, n: int) -> tuple[float, str]:
    """Edge over the better of always-long / always-short, and a verdict against the scorecard tab's
    rough 95% band (overlapping windows count as ~1/HORIZON_BARS of a sample). The band's variance
    uses (hits + 1) / (n + 2), so 0 of 4 is not a zero-width band that calls itself significant."""
    edge = hit - max(long_rate, 1 - long_rate)
    q = (hit * n + 1) / (n + 2)
    band = 1.96 * math.sqrt(q * (1 - q) / max(1, n / HORIZON_BARS))
    return edge, "noise" if abs(edge) <= band else "better" if edge > 0 else "worse"


async def by_source_tf(days: int) -> list[dict]:
    """Live sources only (replay is a backfill, not a track record), asset calls only (sector roll-ups
    are graded against their own index; see /api/scorecard): n, hit rate, always-long rate."""
    pool = await db.pool()
    rows = await pool.fetch(
        f"""SELECT s.source, s.tf, count(*) AS n, avg(o.hit::int)::float AS hit_rate,
                   avg({ROSE})::float AS long_rate
            FROM signals s JOIN outcomes o ON o.signal_id = s.id
            WHERE s.source <> 'quant_replay' AND s.bias <> 0 AND o.hit IS NOT NULL
              AND s.scope NOT LIKE 'sector:%' AND s.as_of > now() - make_interval(days => $1)
            GROUP BY 1, 2""", days)
    return [dict(r) for r in rows]
