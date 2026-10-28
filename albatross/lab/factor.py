"""Factor-rank strategies: how to weigh and measure the signals that rank the liquid Hyperliquid coins against each
other (strategy/factor_rank.py's three, plus momentum), how long to hold, how many coins a side, and long-and-short
or long-only. A strategy is a config; it is judged on non-overlapping holding periods from Hyperliquid's daily candles,
after a trading cost each way (funding is left out, as in the factor books' back-tests).

  long_short   the strongest slice minus the weakest, each equally weighted
  long_only    the strongest slice minus every liquid coin equally weighted (what beating the market by picking means)
  long_hedged  the strongest slice minus BTC and ETH half and half (tradable without shorting the small coins)

The book is the factor paper books' design: a new slice is formed every day and held hold_days, so hold_days slices
are open at once and the book's daily return is their average. Its periods are fixed hold_days blocks counted from
EPOCH. A single grid starting on whichever day the history began moved by a day every day, and the same strategy's t
swung from 0.9 to 2.5 with it (review of 2026-10-09).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from albatross.strategy import factor_rank

PARTS = ("calm", "near_high", "volume", "momentum")
COST_PER_SIDE = 0.001          # Hyperliquid taker fee plus slippage on the liquid coins, each way
HISTORY_DAYS = 760
EPOCH = date(2024, 1, 1)       # periods are hold_days blocks counted from here, the same blocks every day
HEDGE = ("BTC", "ETH")


class FactorConfig(BaseModel):
    weights: dict[Literal["calm", "near_high", "volume", "momentum"], float] = Field(
        default_factory=lambda: {"calm": 1.0, "near_high": 1.0, "volume": 1.0})
    vol_days: Literal[14, 20, 30, 60] = 30
    high_days: Literal[10, 20, 40] = 20
    volume_days: Literal[14, 30, 60] = 30
    mom_days: Literal[5, 14, 30, 60] = 30
    hold_days: Literal[3, 7, 14] = 7
    slice: Literal[3, 5, 10] = 5             # a third, a fifth or a tenth of the coins a side
    side: Literal["long_short", "long_only", "long_hedged"] = "long_short"

    @model_validator(mode="after")
    def _bounded(self):
        if not any(abs(w) > 0 for w in self.weights.values()):
            raise ValueError("at least one weight must be non-zero")
        if any(not -1 <= w <= 1 for w in self.weights.values()):
            raise ValueError("weights run from -1 to 1")
        self.weights = {k: round(float(w), 2) for k, w in self.weights.items() if w}
        return self


def name(cfg: FactorConfig) -> str:
    word = {"calm": "calm", "near_high": "near high", "volume": "volume", "momentum": f"momentum {cfg.mom_days}d"}
    mix = ", ".join(f"{word[k]} {w:+g}" for k, w in sorted(cfg.weights.items()))
    side = {"long_short": "long and short", "long_only": "long only", "long_hedged": "long, hedged with BTC and ETH"}[cfg.side]
    return f"{mix} · {cfg.hold_days}-day hold · top {'third' if cfg.slice == 3 else 'fifth' if cfg.slice == 5 else 'tenth'} · {side}"


async def load() -> tuple[pd.DataFrame, ...]:
    """Closes, highs, lows and dollar volume, days x coins (closed days only)."""
    return await factor_rank._frames(HISTORY_DAYS)


def scores(cfg: FactorConfig, px: pd.DataFrame, hi: pd.DataFrame, lo: pd.DataFrame, dv: pd.DataFrame) -> pd.DataFrame:
    """Each day's score per liquid coin (days x coins, NaN where illiquid or unknown): the weighted z-scores."""
    r1 = px.pct_change(fill_method=None)
    logdv = np.log(dv.where(dv > 0))
    raw = {"calm": -r1.rolling(cfg.vol_days, min_periods=max(10, cfg.vol_days * 2 // 3)).std(),
           "near_high": (px - lo.rolling(cfg.high_days).min()) / (hi.rolling(cfg.high_days).max() - lo.rolling(cfg.high_days).min()),
           "volume": logdv - logdv.rolling(cfg.volume_days, min_periods=max(10, cfg.volume_days * 2 // 3)).mean(),
           "momentum": px / px.shift(cfg.mom_days) - 1}
    liquid = dv.rolling(30, min_periods=20).median() > factor_rank.MIN_DV_USD
    total = None
    for k, w in cfg.weights.items():
        f = raw[k].where(liquid)
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1).replace(0, np.nan), axis=0)
        z = z.mask(f.notna() & z.isna(), 0.0)
        total = z * w if total is None else total + z * w
    return total.where(liquid)


def daily(cfg: FactorConfig, frames: tuple[pd.DataFrame, ...]) -> pd.Series:
    """The book's net return each day (%), on the days all hold_days slices are open. Each slice holds the picks of
    the close it was formed on, equally weighted, and pays a full turn of costs over its hold."""
    px = frames[0]
    s = scores(cfg, *frames)
    r1 = px.pct_change(fill_method=None)
    n = s.notna().sum(axis=1)
    ok = n >= factor_rank.MIN_NAMES
    k = (n // cfg.slice).clip(lower=1)
    rank = s.rank(axis=1, ascending=False, method="first")
    top = rank.le(k, axis=0) & ok.to_numpy()[:, None]
    bottom = rank.gt(n - k, axis=0) & ok.to_numpy()[:, None]
    every = s.notna() & ok.to_numpy()[:, None]
    hedge = r1[[c for c in HEDGE if c in r1.columns]].mean(axis=1)

    def mean_of(mask: pd.DataFrame) -> pd.Series:
        held = mask & r1.notna()
        return r1.where(held).sum(axis=1) / held.sum(axis=1).replace(0, np.nan)

    slices = []
    for j in range(1, cfg.hold_days + 1):          # the slice formed j closes ago
        t = mean_of(top.shift(j, fill_value=False))
        other = (mean_of(bottom.shift(j, fill_value=False)) if cfg.side == "long_short"
                 else mean_of(every.shift(j, fill_value=False)) if cfg.side == "long_only" else hedge)
        slices.append(t - other)
    book = pd.concat(slices, axis=1)
    full = book.notna().all(axis=1)
    cost = (4 if cfg.side == "long_short" else 2) * COST_PER_SIDE / cfg.hold_days
    return ((book.mean(axis=1) - cost) * 100)[full]


def periods(cfg: FactorConfig, frames: tuple[pd.DataFrame, ...]) -> list[tuple[date, float]]:
    """(the first day of each complete hold_days block from EPOCH, the book's net return over it in %)."""
    d = daily(cfg, frames)
    if d.empty:
        return []
    blocks: dict[int, list[float]] = {}
    for t, v in d.items():
        blocks.setdefault((t.date() - EPOCH).days // cfg.hold_days, []).append(float(v))
    return [(EPOCH + timedelta(days=b * cfg.hold_days), sum(vs)) for b, vs in sorted(blocks.items())
            if len(vs) == cfg.hold_days]


def window(ps: list[tuple[date, float]], start: date, end: date) -> list[tuple[float, date]]:
    """(return %, its period) for the periods starting in [start, end): each period is its own cluster."""
    return [(r, d) for d, r in ps if start <= d < end]


def today() -> date:
    return datetime.now(UTC).date()
