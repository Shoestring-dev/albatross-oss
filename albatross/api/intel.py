"""/api/intel: where the edge is, in one call, in plain words.

  edge      every signal source against a naive always-long / always-short caller: proven, not proven,
            too early to tell (grading/scorecard.py), plus the factor ranking's record
  ranking   today's strongest and weakest liquid coins on the factor rank, with the reasons
  record    the factor rank's track record: backtest (backfilled days) and live days, kept apart
  unusual   what stands out right now: volume shocks, fresh 20-day highs and lows, extreme funding
Read-only. Research behind it: docs/RESEARCH-EDGE.md.
"""

import math
import time
from datetime import UTC, datetime

from fastapi import APIRouter

from albatross import db
from albatross.agents import jev_calibration as jc
from albatross.features.engine import HORIZON_BARS
from albatross.grading import relative, scorecard
from albatross.strategy import factor_rank

router = APIRouter(prefix="/api")

SOURCE_NAME = {
    "quant": "Quant model", "quant_replay": "Quant model (5-month replay)", "ensemble": "Blended call",
    "strategist": "Strategist", "ltf_analyst": "LTF analyst", "htf_analyst": "HTF analyst",
    "flow_analyst": "Flow analyst", "macro_analyst": "Macro analyst", "social_pulse": "X sentiment",
    "oracle": "Oracle", "jev_analyst": "Jev (TypeSafe)",
}


def source_name(src: str) -> str:
    """strategist@jev-router -> "Strategist on jev-router (trial)": a model on trial beside the live one."""
    if "@" in src:
        base, model = src.split("@", 1)
        return f"{SOURCE_NAME.get(base, base)} on {model} (trial)"
    return SOURCE_NAME.get(src, src.replace("_", " ").capitalize())
MIN_EFFECTIVE = 30          # graded windows (n / HORIZON_BARS, overlapping windows discounted) before a verdict
HOLD_DAYS = 7               # the ranking is judged on the next 7 days, rebalanced weekly
_cache: dict[str, tuple[float, dict]] = {}


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.monotonic(), val)
    return val


def verdict(n: int, hit: float, long_rate: float) -> tuple[str, str, float]:
    """-> (status, words, edge in points) for one source/timeframe."""
    edge, v = scorecard.vs_naive(hit, long_rate, n)
    if n / HORIZON_BARS < MIN_EFFECTIVE:
        return "early", "too early to tell", edge
    return {"better": ("proven", "better than a one-way caller"), "worse": ("worse", "worse than a one-way caller"),
            "noise": ("none", "no better than a one-way caller")}[v] + (edge,)


async def _edge() -> list[dict]:
    pool = await db.pool()
    rows = await pool.fetch(
        f"""SELECT s.source, s.tf, count(*) AS n, avg(o.hit::int)::float AS hit_rate,
                   avg({scorecard.ROSE})::float AS long_rate, min(s.as_of) AS since
            FROM signals s JOIN outcomes o ON o.signal_id = s.id
            WHERE s.bias <> 0 AND o.hit IS NOT NULL AND s.scope NOT LIKE 'sector:%'
            GROUP BY 1, 2""")
    by_src: dict[str, list] = {}
    for r in rows:
        status, words, edge = verdict(r["n"], r["hit_rate"], r["long_rate"])
        by_src.setdefault(r["source"], []).append({
            "tf": r["tf"], "n": r["n"], "hit_rate": r["hit_rate"], "naive": max(r["long_rate"], 1 - r["long_rate"]),
            "edge_pts": round(edge * 100, 1), "status": status, "words": words, "since": r["since"]})
    rank = {"proven": 0, "worse": 1, "none": 2, "early": 3}
    out = []
    for src, tfs in by_src.items():
        # Headline: a proven timeframe if there is one, else the one with the most graded calls.
        proven = [t for t in tfs if t["status"] == "proven"]
        best = max(proven or tfs, key=lambda t: t["n"])
        out.append({"source": src, "name": source_name(src),
                    "status": best["status"], "words": best["words"], "headline_tf": best["tf"],
                    "tfs": sorted(tfs, key=lambda t: t["tf"])})
    # Coin-picking (relative) grading next to the directional verdict: a source can have one without the other.
    rel: dict[str, list] = {}
    for r in await relative.by_source_tf():
        rel.setdefault(r["source"], []).append(r)
    for s in out:
        tfs = rel.get(s["source"], [])
        best = max([t for t in tfs if t["status"] == "proven"] or tfs, key=lambda t: t["effective_runs"], default=None)
        s["picking"] = None if best is None else {k: best[k] for k in ("tf", "status", "words", "rel_return_pct", "t", "effective_runs")}
        s["picking_tfs"] = tfs
    out.sort(key=lambda s: (rank[s["status"]], s["name"]))
    return out


async def _record() -> dict:
    """Weekly Q5 - Q1 next-7-day return from factor_ranks joined to closes; backfill and live apart."""
    pool = await db.pool()
    rows = await pool.fetch(
        """WITH r AS (SELECT day, symbol, quintile, live FROM factor_ranks
                      WHERE day > now() - interval '400 days'),
                px AS (SELECT symbol, ts::date AS day, close FROM candles
                       WHERE venue = $1 AND tf = '1d' AND ts > now() - interval '410 days')
           SELECT r.day, r.live, r.quintile, avg(p1.close / p0.close - 1) AS ret, count(*) AS k
           FROM r JOIN px p0 ON p0.symbol = r.symbol AND p0.day = r.day
                  JOIN px p1 ON p1.symbol = r.symbol AND p1.day = r.day + $2::int
           WHERE r.quintile IN (1, 5) GROUP BY 1, 2, 3 ORDER BY 1""", factor_rank.VENUE, HOLD_DAYS)
    weeks: dict[tuple, dict] = {}
    for r in rows:
        weeks.setdefault((r["day"], r["live"]), {})[r["quintile"]] = r["ret"]
    out = {}
    for live in (False, True):
        # one ranking a week (non-overlapping 7-day holds), counted from that group's first ranked day
        first = min((d for d, lv in weeks if lv is live), default=None)
        spreads = [w[5] - w[1] for (d, lv), w in sorted(weeks.items())
                   if lv is live and 5 in w and 1 in w and (d - first).days % HOLD_DAYS == 0]
        n = len(spreads)
        mean = sum(spreads) / n if n else None
        sd = math.sqrt(sum((s - mean) ** 2 for s in spreads) / (n - 1)) if n > 1 else None
        out["live" if live else "backtest"] = {
            "weeks": n, "mean_spread_pct": None if mean is None else round(mean * 100, 2),
            "won_pct": None if not n else round(100 * sum(s > 0 for s in spreads) / n),
            "t": None if not sd else round(mean / sd * math.sqrt(n), 1)}
    live_since = await pool.fetchval("SELECT min(day) FROM factor_ranks WHERE live")
    out["live_since"] = live_since
    return out


async def _ranking() -> dict | None:
    pool = await db.pool()
    day = await pool.fetchval("SELECT max(day) FROM factor_ranks")
    if not day:
        return None
    rows = [dict(r) for r in await pool.fetch(
        "SELECT * FROM factor_ranks WHERE day = $1 ORDER BY rank", day)]
    vols = sorted(r["vol_30d"] for r in rows if r["vol_30d"] is not None)
    median_vol = vols[len(vols) // 2] if vols else None
    for r in rows:
        r["reasons"] = factor_rank.reasons(r, median_vol)
    return {"day": day, "n": len(rows), "strongest": rows[:5], "weakest": rows[-5:][::-1],
            "all": {r["symbol"]: {"rank": r["rank"], "quintile": r["quintile"], "reasons": r["reasons"]} for r in rows}}


async def _unusual(ranking: dict | None) -> list[dict]:
    out = []
    pool = await db.pool()
    if ranking:
        rows = [dict(r) for r in await pool.fetch("SELECT * FROM factor_ranks WHERE day = $1", ranking["day"])]
        for r in rows:
            if r["volume"] is not None and math.exp(r["volume"]) >= 2:
                out.append({"symbol": r["symbol"], "kind": "volume", "tone": "accent",
                            "text": f"volume {math.exp(r['volume']):.1f}× its 30-day norm"})
            if r["near_high"] is not None and r["near_high"] >= 0.99:
                out.append({"symbol": r["symbol"], "kind": "high", "tone": "bull", "text": "closed at a fresh 20-day high"})
            elif r["near_high"] is not None and r["near_high"] <= 0.01:
                out.append({"symbol": r["symbol"], "kind": "low", "tone": "bear", "text": "closed at a fresh 20-day low"})
    for r in await pool.fetch(
        """SELECT DISTINCT ON (symbol) symbol, funding_rate FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND ts > now() - interval '15 minutes' ORDER BY symbol, ts DESC"""):
        if r["funding_rate"] is None:
            continue
        apr = r["funding_rate"] * 24 * 365 * 100
        if apr >= 60 or apr <= -30:
            out.append({"symbol": r["symbol"], "kind": "funding", "tone": "bear" if apr > 0 else "bull",
                        "text": f"funding {apr:+.0f}% a year: {'longs' if apr > 0 else 'shorts'} are paying up (crowded)"})
    order = {"funding": 0, "volume": 1, "high": 2, "low": 3}
    return sorted(out, key=lambda x: (order[x["kind"]], x["symbol"]))[:12]


@router.get("/intel")
async def intel():
    async def build():
        ranking = await _ranking()
        return {"as_of": datetime.now(UTC), "edge": await _edge(), "ranking": ranking,
                "record": await _record(), "unusual": await _unusual(ranking),
                "note": "The ranking compares coins with each other; nothing in our data predicts where the whole market goes next."}
    return await _cached("intel", 300, build)



# ─── Jev trade scout (albatross/agents/jev_scout.py): calibration and the paper book ─────────────────────
BUCKETS = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]


@router.get("/jev")
async def jev():
    async def build():
        from albatross.config import settings
        pool = await db.pool()
        if not await pool.fetchval("SELECT to_regclass('public.jev_scores') IS NOT NULL"):
            return None
        counts = await pool.fetchrow(
            """SELECT count(*) AS scored, count(*) FILTER (WHERE scored_at > now() - interval '24 hours') AS today,
                      count(*) FILTER (WHERE outcome = 'graded') AS graded, count(*) FILTER (WHERE triggered) AS taken,
                      min(scored_at) AS since FROM jev_scores""")
        cal = []
        for lo, hi in BUCKETS:
            r = await pool.fetchrow(
                """SELECT count(*) AS n, avg((exit_reason = 'target')::int)::float AS won, avg(r_multiple) AS avg_r
                   FROM jev_scores WHERE outcome = 'graded' AND p_win >= $1 AND p_win < $2""", lo, hi)
            cal.append({"from": lo, "to": min(hi, 1.0), "n": r["n"], "won": r["won"], "avg_r": r["avg_r"]})
        # The correction (albatross/agents/jev_calibration.py), per timeframe over the window it is fit on: what
        # Jev said, what won, what a 40% now reads as, and the out-of-sample check: candidates scored with the
        # corrected number (p_cal) that have since been graded.
        rows = {r["tf"]: dict(r) for r in await pool.fetch(
            f"""SELECT tf, count(*) AS n, avg(p_win)::float AS said, avg((exit_reason = 'target')::int)::float AS won,
                       count(p_cal) AS n_cal, avg(p_cal)::float AS cal,
                       (avg((exit_reason = 'target')::int) FILTER (WHERE p_cal IS NOT NULL))::float AS cal_won
                FROM jev_scores WHERE outcome = 'graded' AND graded_at > now() - interval '{jc.WINDOW}' GROUP BY tf""")}
        c = await jc.load()
        fitted = await pool.fetchval("SELECT max(updated_at) FROM jev_calibration")
        correction = [{"tf": tf, "n": 0, "n_cal": 0, **rows.get(tf, {}), "reads_40": c(0.40, tf), "map": c.source(tf)}
                      for tf in jc.ORDER]
        book = await pool.fetchrow(
            """SELECT count(*) FILTER (WHERE c.horizon_until > now() AND o.status IS DISTINCT FROM 'graded') AS open,
                      count(*) FILTER (WHERE o.status = 'graded') AS closed,
                      avg((o.exit_reason = 'target')::int) FILTER (WHERE o.status = 'graded')::float AS won,
                      avg(o.r_multiple) FILTER (WHERE o.status = 'graded') AS avg_r,
                      sum(o.r_multiple) FILTER (WHERE o.status = 'graded') AS total_r
               FROM trade_calls c LEFT JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'stop_target'
               WHERE c.source_kind = 'model' AND c.source_id = 'jev'""")
        return {"counts": dict(counts), "calibration": cal, "book": dict(book),
                "correction": {"by_tf": correction, "fitted_at": fitted, "window": jc.WINDOW, "half_life_h": jc.HALF_LIFE_H},
                "rules": {"min_p": settings.jev_scout_min_p, "min_ev": settings.jev_scout_min_ev,
                          "max_open": settings.jev_scout_max_open, "stop_atr": 1.5, "reward_risk": 2.0}}
    return await _cached("jev", 120, build)
