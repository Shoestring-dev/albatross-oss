"""The lab's statistics: a mean with a standard error clustered by day (trades on one day move together), and the bar
a strategy has to clear, which rises with how many strategies have been tried so that testing more can't manufacture
a winner (about the largest t that many tries of a strategy with no edge would reach by luck)."""

import math
from collections import defaultdict


def clustered(values: list[float], clusters: list) -> dict | None:
    """{n, mean, se, t, clusters}: the mean of `values` with a cluster-robust standard error. None for fewer than two."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    by: dict = defaultdict(float)
    for v, c in zip(values, clusters, strict=True):
        by[c] += v - mean
    g = len(by)
    se = math.sqrt(sum(s * s for s in by.values()) * g / (g - 1)) / n if g > 1 else None
    t = mean / se if se else None
    return {"n": n, "mean": round(mean, 4), "se": round(se, 4) if se else None,
            "t": round(t, 3) if t is not None else None, "clusters": g}


def t_needed(tried: int) -> float:
    """The t a strategy must reach when `tried` strategies of its family have been tested: 2 at first, then
    sqrt(2 ln N) (3.0 at 100 tried, 3.7 at 1,000)."""
    return round(max(2.0, math.sqrt(2 * math.log(max(tried, 1)))), 3)
