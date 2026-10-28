"""Jev's probabilities, corrected against what actually happened (jev_scout's graded candidates).

Jev ranks trades in the right order but states its odds about 2.7x too high: by 2026-09-30, across 31,698 graded
candidates (14 days), it said 24% on average and 9% reached the target; 15m candidates it put at 40-45% won 24%.
So before any expected R or paper trade, a raw probability goes through this map. Per timeframe, the graded candidates are
binned by what Jev said, and each bin's hit rate is shrunk toward its timeframe's overall rate (PRIOR candidates'
worth, so a thin bin can't swing it). The result is then made to rise with what Jev said (pool-adjacent-violators:
more confidence never maps to less). A timeframe with fewer than MIN_N graded candidates borrows the map of the
nearest timeframe that has one (4h and 1d borrow 1h's until their own grades land), then the map of all
timeframes together. With no map at all, the raw number is scaled by the overall hit/said ratio.

Recent grades count most, because how often a 2R target is reached swings with the market within hours (15m: 8% in
the morning of 2026-09-30, 23.5% around the PCE print, 1-4% overnight). A graded candidate counts 0.5^(age /
HALF_LIFE_H[tf]), its age taken from when it was scored. The half-lives come from a walk-forward replay of the graded
record (albatross/agents/jev_replay.py; rerun it as grades accumulate). On the first 25 hours, a 3 h memory brought
15m's predicted level from 9.8% to 8.8% against 8.2% that won, and it left 5m unchanged. On 1h a shorter memory did
not help, because its outcomes arrive 6 hours late and a short memory chases a regime that has already passed. So 5m
and 15m remember about 3 hours, and 1h and up about two days.

The map is refit after every grading pass (jev_grade) from the last WINDOW of graded candidates and stored in
jev_calibration (n and hits there are the weighted counts it rests on). jev_scores keeps both the raw p_win and the
p_cal that was used, so the correction can itself be checked on candidates scored after it (GET /api/jev).
"""

from bisect import bisect_right
from dataclasses import dataclass

from albatross import db

WIDTH = 0.05                # bins of raw probability
PRIOR = 20                  # shrinkage toward the timeframe's (weighted) hit rate, in candidates' worth
MIN_N = 300                 # weighted graded candidates a timeframe needs for its own map
WINDOW = "14 days"
ALL = "all"
ORDER = ["5m", "15m", "1h", "4h", "1d"]         # every timeframe the scout has scored: the nearest map is borrowed in this order
HALF_LIFE_H = {"5m": 3.0, "15m": 3.0, "1h": 48.0, "4h": 48.0, "1d": 48.0}   # memory per timeframe (jev_replay)
DEFAULT_HALF_LIFE_H = 48.0


@dataclass
class Bin:
    lo: float
    hi: float
    n: float                    # graded candidates' weight in the bin
    hits: float                 # of which reached the target first
    p: float = 0.0


def pav(bins: list[Bin]) -> list[Bin]:
    """Pool adjacent violators (weighted by n): make p non-decreasing across ascending bins."""
    blocks: list[list] = []                      # [p, weight, members]
    for b in bins:
        blocks.append([b.p, b.n, [b]])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            p2, w2, m2 = blocks.pop()
            p1, w1, m1 = blocks.pop()
            blocks.append([(p1 * w1 + p2 * w2) / (w1 + w2), w1 + w2, m1 + m2])
    for p, _, members in blocks:
        for b in members:
            b.p = p
    return bins


def fit(rows: list[tuple[float, float, float]]) -> list[Bin]:
    """rows: (bin lower edge, weight, weighted hits) for one timeframe -> the monotone map, bins ascending."""
    n_all, h_all = sum(n for _, n, _ in rows), sum(h for _, _, h in rows)
    base = h_all / n_all if n_all else 0.0
    return pav([Bin(lo, round(lo + WIDTH, 4), n, h, (h + PRIOR * base) / (n + PRIOR)) for lo, n, h in sorted(rows) if n])


def apply(p: float, bins: list[Bin]) -> float:
    """Piecewise-linear through the bin centres; flat beyond the outermost ones."""
    if not bins:
        return p
    centres = [(b.lo + b.hi) / 2 for b in bins]
    if p <= centres[0]:
        return bins[0].p
    if p >= centres[-1]:
        return bins[-1].p
    i = bisect_right(centres, p)
    x0, x1, y0, y1 = centres[i - 1], centres[i], bins[i - 1].p, bins[i].p
    return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


@dataclass
class Calibration:
    maps: dict[str, list[Bin]]
    ratio: float = 1.0                          # overall hit/said: the fallback when there is no map at all

    def source(self, tf: str) -> str | None:
        """Whose map a timeframe uses: its own, the nearest timeframe's, all timeframes', or none."""
        if tf in self.maps:
            return tf
        if tf in ORDER:
            near = sorted((t for t in self.maps if t in ORDER),
                          key=lambda t: (abs(ORDER.index(t) - ORDER.index(tf)), -ORDER.index(t)))
            if near:
                return near[0]
        return ALL if ALL in self.maps else None

    def __call__(self, p: float, tf: str) -> float:
        src = self.source(tf)
        return round(apply(p, self.maps[src]) if src else p * self.ratio, 4)


# A graded candidate's weight: 0.5^(hours since it was scored / its timeframe's half-life). The exponent is capped
# so that Postgres never underflows (it raises rather than returning 0).
WEIGHT_SQL = """power(0.5::float8, least(extract(epoch FROM now() - s.scored_at)::float8 / 3600
                                          / coalesce(h.half_h, $3), 1000))"""


async def refit() -> dict[str, dict]:
    """Refit every timeframe's map from the last WINDOW of graded candidates, recent ones weighted most
    -> {tf: {"graded": candidates, "weight": what they count for}}."""
    pool = await db.pool()
    tfs = list(HALF_LIFE_H)
    rows = await pool.fetch(
        f"""SELECT s.tf, floor(s.p_win / {WIDTH}) * {WIDTH} AS lo, count(*) AS n, sum({WEIGHT_SQL}) AS w,
                   coalesce(sum({WEIGHT_SQL}) FILTER (WHERE s.exit_reason = 'target'), 0) AS wh
            FROM jev_scores s LEFT JOIN unnest($1::text[], $2::float8[]) AS h(tf, half_h) ON h.tf = s.tf
            WHERE s.outcome = 'graded' AND s.graded_at > now() - interval '{WINDOW}'
            GROUP BY 1, 2""", tfs, [HALF_LIFE_H[t] for t in tfs], DEFAULT_HALF_LIFE_H)
    by: dict[str, dict[float, list[float]]] = {}
    for r in rows:
        lo = round(float(r["lo"]), 4)
        for key in (r["tf"], ALL):
            m = by.setdefault(key, {}).setdefault(lo, [0, 0.0, 0.0])
            m[0] += r["n"]
            m[1] += float(r["w"])
            m[2] += float(r["wh"])
    out, counts = [], {}
    for tf, bins in by.items():
        weight = sum(w for _, w, _ in bins.values())
        counts[tf] = {"graded": sum(n for n, _, _ in bins.values()), "weight": round(weight)}
        if tf != ALL and weight < MIN_N:
            continue
        out += [(tf, b.lo, b.hi, round(b.n, 3), round(b.hits, 3), round(b.p, 5))
                for b in fit([(lo, w, wh) for lo, (_, w, wh) in bins.items()])]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM jev_calibration")
        await conn.executemany(
            """INSERT INTO jev_calibration (tf, bin_lo, bin_hi, n, hits, p_cal, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, now())""", out)
    return counts


async def load() -> Calibration:
    pool = await db.pool()
    maps: dict[str, list[Bin]] = {}
    for r in await pool.fetch("SELECT tf, bin_lo, bin_hi, n, hits, p_cal FROM jev_calibration ORDER BY tf, bin_lo"):
        maps.setdefault(r["tf"], []).append(Bin(r["bin_lo"], r["bin_hi"], r["n"], r["hits"], r["p_cal"]))
    r = await pool.fetchrow(
        f"""SELECT avg(p_win) AS said, avg((exit_reason = 'target')::int) AS won FROM jev_scores
            WHERE outcome = 'graded' AND graded_at > now() - interval '{WINDOW}'""")
    return Calibration(maps, float(r["won"]) / float(r["said"]) if r["said"] and r["won"] is not None else 1.0)
