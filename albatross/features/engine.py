"""Turns stored candles into per-timeframe features + quant signals, then rolls assets up into
sector signals. Higher timeframes are resampled from stored bases:

    5m  -> 5m, 15m        1h -> 1h, 4h        1d -> 1d, 1w (weeks start Monday 00:00 UTC)
"""

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

import orjson
import pandas as pd

from albatross import db
from albatross.features import indicators, scoring

log = logging.getLogger(__name__)

# derived tf -> (stored base tf, pandas rule, bar length, base bars to load)
TFS = {
    "5m": ("5m", None, timedelta(minutes=5), 320),
    "15m": ("5m", "15min", timedelta(minutes=15), 960),
    "1h": ("1h", None, timedelta(hours=1), 320),
    "4h": ("1h", "4h", timedelta(hours=4), 1280),
    "1d": ("1d", None, timedelta(days=1), 320),
    "1w": ("1d", "W-MON", timedelta(weeks=1), 1500),
}
# A signal is judged over this many of its own bars.
HORIZON_BARS = 6
VENUE_PREFERENCE = ["hyperliquid", "okx", "coinbase"]


async def _assets(tiers: list[int]) -> list:
    pool = await db.pool()
    return await pool.fetch(
        "SELECT symbol, primary_category, volume_24h_usd FROM assets WHERE active AND tier = ANY($1::smallint[])",
        tiers,
    )


async def _candles(symbol: str, base_tf: str, limit: int) -> tuple[str, pd.DataFrame] | None:
    pool = await db.pool()
    for venue in VENUE_PREFERENCE:
        rows = await pool.fetch(
            """SELECT ts, open, high, low, close, volume FROM candles
               WHERE venue = $1 AND symbol = $2 AND tf = $3 ORDER BY ts DESC LIMIT $4""",
            venue, symbol, base_tf, limit,
        )
        if len(rows) >= 30:
            df = pd.DataFrame([dict(r) for r in rows]).set_index("ts").sort_index()
            return venue, df
    return None


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["close"])


def _closed(df: pd.DataFrame, bar: timedelta) -> pd.DataFrame:
    now = datetime.now(UTC)
    return df[df.index + bar <= now]


async def compute_asset(symbol: str, tfs: list[str]) -> list[tuple[str, dict, tuple]]:
    """-> [(tf, feature row, signal row)] for one asset."""
    out = []
    bases: dict[str, tuple[str, pd.DataFrame] | None] = {}
    for tf in tfs:
        base_tf, rule, bar, need = TFS[tf]
        key = f"{base_tf}:{need}"
        if key not in bases:
            bases[key] = await _candles(symbol, base_tf, need)
        loaded = bases[key]
        if loaded is None:
            continue
        venue, df = loaded
        df = _resample(df, rule) if rule else df
        # A partial first bucket from the resample would distort the oldest bar; drop it.
        df = _closed(df.iloc[1:] if rule else df, bar)
        f = indicators.compute(df)
        if f is None:
            continue
        ts = df.index[-1].to_pydatetime()
        s, bias, conf, comps = scoring.score(f)
        invalidation = f["last_swing_low"] if bias > 0 else f["last_swing_high"] if bias < 0 else None
        feature_row = (symbol, tf, ts, venue, f["close"], orjson.dumps(f).decode())
        signal_row = (
            symbol, tf, bias, conf, f["close"], invalidation,
            orjson.dumps({"components": comps, "structure": f["structure"], "ema_stack": f["ema_stack"],
                          "rsi14": f["rsi14"]}).decode(),
            ts + bar * (1 + HORIZON_BARS), "quant", ts, s,
        )
        out.append((tf, feature_row, signal_row))
    return out


async def run(tfs: list[str], tiers: list[int]) -> int:
    assets = await _assets(tiers)
    sem = asyncio.Semaphore(8)

    async def one(a):
        async with sem:
            return a, await compute_asset(a["symbol"], tfs)

    results = await asyncio.gather(*(one(a) for a in assets), return_exceptions=True)
    features, signals = [], []
    by_sector: dict[tuple[str, str], list[tuple[float, float, datetime]]] = {}
    for r in results:
        if isinstance(r, Exception):
            log.warning("feature compute failed: %s: %s", type(r).__name__, r)
            continue
        a, rows = r
        weight = math.sqrt(a["volume_24h_usd"] or 0) or 1.0
        for tf, f_row, s_row in rows:
            features.append(f_row)
            signals.append(s_row)
            by_sector.setdefault((a["primary_category"] or "other", tf), []).append((s_row[-1], weight, s_row[-2]))

    await db.upsert("features", ["symbol", "tf", "ts", "venue", "close", "data"], features,
                    ["symbol", "tf", "ts"])
    sig_cols = ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
                "source", "as_of", "score"]
    await db.upsert("signals", sig_cols, signals, ["scope", "tf", "source", "as_of"])
    await db.upsert("signals", sig_cols, _sector_signals(by_sector), ["scope", "tf", "source", "as_of"])
    return len(features)


async def replay(tfs: list[str], tiers: list[int], bars: int = 180) -> int:
    """Walk-forward: recompute the quant signal at each of the last `bars` closed candles,
    using only data available at that time (same lookback window as live). Tagged
    source='quant_replay' so it never mixes with live calls."""
    assets = await _assets(tiers)
    window = 320                                       # same lookback live signals use
    rows, total = [], 0
    for a in assets:
        for tf in tfs:
            base_tf, rule, bar, _ = TFS[tf]
            per_bar = int(bar / TFS[base_tf][2])       # base candles per derived candle
            loaded = await _candles(a["symbol"], base_tf, (window + bars + 1) * per_bar)
            if loaded is None:
                continue
            _, df = loaded
            df = _resample(df, rule).iloc[1:] if rule else df
            df = _closed(df, bar)
            # Start once there's enough history for the slower indicators (live degrades the same way).
            for i in range(max(60, len(df) - bars), len(df)):
                f = indicators.compute(df.iloc[max(0, i - window + 1):i + 1])
                if f is None:
                    continue
                ts = df.index[i].to_pydatetime()
                s, bias, conf, comps = scoring.score(f)
                inval = f["last_swing_low"] if bias > 0 else f["last_swing_high"] if bias < 0 else None
                rows.append((
                    a["symbol"], tf, bias, conf, f["close"], inval,
                    orjson.dumps({"components": comps}).decode(),
                    ts + bar * (1 + HORIZON_BARS), "quant_replay", ts, s,
                ))
        if len(rows) > 5000:
            await _flush_replay(rows)
            total += len(rows)
            rows = []
    await _flush_replay(rows)
    return total + len(rows)


async def _flush_replay(rows: list[tuple]) -> None:
    await db.upsert(
        "signals",
        ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
         "source", "as_of", "score"],
        rows, ["scope", "tf", "source", "as_of"],
    )


def _sector_signals(by_sector) -> list[tuple]:
    """Volume-weighted mean of member scores; breadth = share of members pointing the same way."""
    rows = []
    for (sector, tf), members in by_sector.items():
        if len(members) < 3:
            continue
        total_w = sum(w for _, w, _ in members)
        s = sum(sc * w for sc, w, _ in members) / total_w
        bias = max(-2, min(2, round(s * 2.5)))
        breadth = sum(1 for sc, _, _ in members if sc * s > 0) / len(members)
        as_of = max(t for _, _, t in members)
        rows.append((
            f"sector:{sector}", tf, bias, round(breadth, 3), None, None,
            orjson.dumps({"members": len(members), "breadth": round(breadth, 3)}).decode(),
            as_of + TFS[tf][2] * (1 + HORIZON_BARS), "quant", as_of, round(s, 4),
        ))
    return rows
