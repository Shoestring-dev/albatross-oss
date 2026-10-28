"""Hyperliquid public info API (no key needed). Primary perps source for US-based ingestion."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from albatross.http import WeightLimiter, client, retrying

URL = "https://api.hyperliquid.xyz/info"
VENUE = "hyperliquid"

# HL allows 1200 weight/min per IP; stay well under it (1000 in all). Candles draw from their own bucket, so the
# every-minute snapshot (derivs_hl, weight 2) never waits behind a first start's year of candles: it once took 93 s.
limiter = WeightLimiter(per_minute=50)               # metaAndAssetCtxs: the snapshot
candle_limiter = WeightLimiter(per_minute=950)

_INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

_K_PREFIX = re.compile(r"^k([A-Z0-9]+)$")


@dataclass
class Perp:
    venue_symbol: str       # as HL names it, e.g. kPEPE
    symbol: str             # canonical, e.g. PEPE
    multiplier: float       # kPEPE price = 1000 * PEPE price
    delisted: bool


def canonical(name: str) -> tuple[str, float]:
    m = _K_PREFIX.match(name)
    if m:
        return m.group(1), 1000.0
    return name.upper(), 1.0


@retrying
async def _post(body: dict, weight: float = 20, bucket: WeightLimiter | None = None) -> object:
    await (bucket or limiter).acquire(weight)
    r = await client().post(URL, json=body)
    r.raise_for_status()
    return r.json()


async def perps_with_contexts() -> list[tuple[Perp, dict]]:
    """One call: every perp plus funding, OI, mark, volume."""
    meta, ctxs = await _post({"type": "metaAndAssetCtxs"}, weight=2)
    out = []
    for asset, ctx in zip(meta["universe"], ctxs, strict=True):
        sym, mult = canonical(asset["name"])
        out.append((Perp(asset["name"], sym, mult, bool(asset.get("isDelisted"))), ctx))
    return out


async def candles(venue_symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Returns at most 5000 most recent candles in the window. interval: 5m, 1h, 1d..."""
    expected = min(5000, (end_ms - start_ms) / 1000 / _INTERVAL_SECONDS[interval] + 1)
    rows = await _post(
        {
            "type": "candleSnapshot",
            "req": {"coin": venue_symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        },
        weight=20 + expected / 60,  # HL charges extra weight per 60 candles returned
        bucket=candle_limiter,
    )
    return rows or []


def candle_row(c: dict, symbol: str, tf: str, multiplier: float) -> tuple:
    """HL candle -> candles table row, with k-prefixed prices normalised to per-unit."""
    m = multiplier
    return (
        VENUE,
        "perp",
        symbol,
        tf,
        datetime.fromtimestamp(c["t"] / 1000, UTC),
        float(c["o"]) / m,
        float(c["h"]) / m,
        float(c["l"]) / m,
        float(c["c"]) / m,
        float(c["v"]) * m,
        int(c.get("n") or 0),
    )
