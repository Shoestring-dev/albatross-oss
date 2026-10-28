"""Factor rank: a daily cross-sectional ranking of the liquid Hyperliquid perps on the three signals that
held up in our own history (research 2026-09-30, docs/RESEARCH-EDGE.md):

  calm         low 30-day volatility of daily returns (the low-volatility effect)
  near_high    where the close sits in its 20-day high-low range (1 = at the high)
  volume       today's dollar volume vs its 30-day norm (log ratio)

Each part is z-scored across the day's liquid coins and averaged. It ranks coins against each other; it
says nothing about where the whole market goes (no timing signal in our data did either). The top fifth
beat the bottom fifth by ~1.9% a week after trading costs over 2025-26 (IC 0.15 at 7 days, t 5.3).

Deterministic, from stored 1d candles only (closed candles: the row for day d uses d's close, so it is
known at d+1 00:00 UTC). `live` marks rankings computed on their own day; backfilled days are history,
kept apart so the live track record stays honest.
"""

import logging
import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from albatross import db

log = logging.getLogger(__name__)

VENUE = "hyperliquid"
MIN_DV_USD = 5e6            # tradeable: 30-day median dollar volume
MIN_NAMES = 12              # fewer liquid coins than this and a quintile is a coin or two: don't rank
LOOKBACK_DAYS = 260
PARTS = ("calm", "near_high", "volume")


def compute(px: pd.DataFrame, hi: pd.DataFrame, lo: pd.DataFrame, dv: pd.DataFrame) -> pd.DataFrame:
    """Wide frames (index day, columns symbol) -> long frame of (day, symbol, parts, score, rank, n,
    quintile) for liquid coins, one ranking per day."""
    r1 = px.pct_change(fill_method=None)
    vol30 = r1.rolling(30, min_periods=20).std()
    dvm = dv.rolling(30, min_periods=20).median()
    logdv = np.log(dv.where(dv > 0))
    raw = {
        "calm": -vol30,
        "near_high": (px - lo.rolling(20).min()) / (hi.rolling(20).max() - lo.rolling(20).min()),
        "volume": logdv - logdv.rolling(30, min_periods=20).mean(),
    }
    liquid = dvm > MIN_DV_USD
    z = {}
    for k, f in raw.items():
        f = f.where(liquid)
        zk = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1).replace(0, np.nan), axis=0)
        z[k] = zk.mask(f.notna() & zk.isna(), 0.0)          # a part with no spread that day says nothing
    score = sum(z.values()) / len(z)
    out = []
    for day in score.index:
        s = score.loc[day].dropna()
        if len(s) < MIN_NAMES:
            continue
        rank = s.rank(ascending=False, method="first").astype(int)
        quint = pd.qcut(s.rank(method="first"), 5, labels=False).astype(int) + 1     # 5 = strongest fifth
        for sym in s.index:
            out.append({"day": day.date(), "symbol": sym, "score": float(s[sym]), "rank": int(rank[sym]),
                        "n": len(s), "quintile": int(quint[sym]),
                        "calm": _f(raw["calm"].at[day, sym]), "near_high": _f(raw["near_high"].at[day, sym]),
                        "volume": _f(raw["volume"].at[day, sym]),
                        "vol_30d": _f(vol30.at[day, sym]), "dv_30d": _f(dvm.at[day, sym])})
    return pd.DataFrame(out)


def _f(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


async def _frames(days: int) -> tuple[pd.DataFrame, ...]:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT symbol, ts, high, low, close, volume FROM candles
           WHERE venue = $1 AND tf = '1d' AND ts >= now() - make_interval(days => $2) ORDER BY ts""",
        VENUE, days)
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return (pd.DataFrame(),) * 4
    df["ts"] = pd.to_datetime(df.ts, utc=True).dt.tz_convert(None).dt.normalize()
    df["dv"] = df.close * df.volume
    wide = {k: df.pivot_table(index="ts", columns="symbol", values=k).sort_index() for k in ("close", "high", "low", "dv")}
    today = pd.Timestamp(datetime.now(UTC).date())
    closed = wide["close"].index < today                  # today's candle is still open
    return tuple(wide[k][closed] for k in ("close", "high", "low", "dv"))


COLS = ["day", "symbol", "score", "rank", "n", "quintile", "calm", "near_high", "volume", "vol_30d", "dv_30d", "live"]


async def run(backfill_days: int = 0) -> int:
    """Rank the last closed day (live). backfill_days > 0 also (re)writes that many earlier days as
    history (live = false), never overwriting a live ranking."""
    px, hi, lo, dv = await _frames(LOOKBACK_DAYS + backfill_days)
    if px.empty:
        return 0
    ranks = compute(px, hi, lo, dv)
    if ranks.empty:
        return 0
    last = ranks.day.max()
    keep = ranks[ranks.day >= last - timedelta(days=backfill_days)] if backfill_days else ranks[ranks.day == last]
    live_day = datetime.now(UTC).date() - timedelta(days=1)         # the candle that closed at 00:00 today
    rows = [tuple(r[c] for c in COLS[:-1]) + (r["day"] == live_day and not backfill_days,)
            for r in keep.to_dict("records")]
    pool = await db.pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            f"""INSERT INTO factor_ranks ({", ".join(COLS)}) VALUES ({", ".join(f"${i + 1}" for i in range(len(COLS)))})
                ON CONFLICT (day, symbol) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in COLS[2:-1])},
                    live = factor_ranks.live OR EXCLUDED.live
                WHERE NOT factor_ranks.live""", rows)
    log.info("factor_rank: %d rows for %s..%s", len(rows), keep.day.min(), keep.day.max())
    return len(rows)


def reasons(r: dict, median_vol: float | None) -> list[str]:
    """Plain words for why a coin ranks where it does."""
    out = []
    if r.get("near_high") is not None:
        nh = r["near_high"]
        out.append("at its 20-day high" if nh >= 0.97 else "near its 20-day high" if nh >= 0.8
                   else "at its 20-day low" if nh <= 0.03 else "near its 20-day low" if nh <= 0.2
                   else f"{round(nh * 100)}% up its 20-day range")
    if r.get("volume") is not None:
        x = math.exp(r["volume"])
        if x >= 1.5 or x <= 0.67:
            out.append(f"volume {x:.1f}× normal")
    if r.get("vol_30d") is not None and median_vol:
        ratio = r["vol_30d"] / median_vol
        if ratio <= 0.8:
            out.append("calmer than most")
        elif ratio >= 1.4:
            out.append("wilder than most")
    return out


def day_label(d: date) -> str:
    return d.strftime("%b %-d")
