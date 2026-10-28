"""Pure indicator math on an OHLCV DataFrame (index = candle open time, UTC).

Only closed candles should be passed in; callers drop the still-forming bar.
"""

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def swings(df: pd.DataFrame, k: int = 3) -> tuple[list[float], list[float]]:
    """Confirmed swing highs/lows: a bar that is the extreme of the k bars on each side."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            highs.append(float(h[i]))
        if l[i] == l[i - k:i + k + 1].min():
            lows.append(float(l[i]))
    return highs, lows


def structure(df: pd.DataFrame) -> str:
    """HH+HL = up, LH+LL = down, otherwise range — from the last two confirmed swings."""
    highs, lows = swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return "up"
    if not hh and not hl:
        return "down"
    return "range"


def compute(df: pd.DataFrame) -> dict | None:
    """Feature snapshot for the latest closed candle. Needs ~210 bars for EMA200; degrades gracefully."""
    if len(df) < 30:
        return None
    c = df["close"]
    last = float(c.iloc[-1])
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    macd = ema(c, 12) - ema(c, 26)
    macd_hist = macd - ema(macd, 9)
    a = atr(df)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    vol = df["volume"]
    vol_z = (vol - vol.rolling(50).mean()) / vol.rolling(50).std()
    highs, lows = swings(df)

    def val(s: pd.Series) -> float | None:
        v = s.iloc[-1]
        return None if pd.isna(v) else round(float(v), 8)

    def pct(a_: float | None, b_: float | None) -> float | None:
        return None if a_ is None or b_ in (None, 0) else round((a_ / b_ - 1) * 100, 4)

    ema20, ema50, ema200 = val(e20), val(e50), val(e200)
    return {
        "close": last,
        "ret_1": pct(last, float(c.iloc[-2])),
        "ret_5": pct(last, float(c.iloc[-6])) if len(c) > 6 else None,
        "ret_20": pct(last, float(c.iloc[-21])) if len(c) > 21 else None,
        "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "dist_ema20_pct": pct(last, ema20),
        "dist_ema50_pct": pct(last, ema50),
        "dist_ema200_pct": pct(last, ema200),
        "ema20_slope_pct": pct(ema20, val(e20.shift(5))),
        "ema_stack": (
            "bull" if ema20 and ema50 and ema200 and ema20 > ema50 > ema200 else
            "bear" if ema20 and ema50 and ema200 and ema20 < ema50 < ema200 else "mixed"
        ),
        "rsi14": val(rsi(c)),
        "macd_hist": val(macd_hist),
        "macd_hist_rising": bool(macd_hist.iloc[-1] > macd_hist.iloc[-2]) if not pd.isna(macd_hist.iloc[-2]) else None,
        "atr_pct": pct(last + (val(a) or 0), last),
        "bb_pctb": None if pd.isna(sd.iloc[-1]) or sd.iloc[-1] == 0 else round(float((last - (mid.iloc[-1] - 2 * sd.iloc[-1])) / (4 * sd.iloc[-1])), 4),
        "vol_z": val(vol_z),
        "hi_20": float(df["high"].iloc[-20:].max()),
        "lo_20": float(df["low"].iloc[-20:].min()),
        "structure": structure(df),
        "last_swing_high": highs[-1] if highs else None,
        "last_swing_low": lows[-1] if lows else None,
        "bars": len(df),
    }
