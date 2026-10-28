"""Coinalyze (free key): Binance/Bybit perp open interest, funding, liquidations, long/short and
taker flow, the Binance/Bybit derivatives data a US user can't pull from those venues directly.

Limits: 40 calls/min per key, and every *symbol* in a request costs one call (max 20 per request).
Intraday history is kept for only ~1,500-2,000 points, so we store what we pull.
Funding values are percent per funding interval (8h on these venues): 0.01 = 0.01%/8h.
"""

import asyncio
import logging

from albatross.config import settings
from albatross.http import WeightLimiter, client, raise_for_status, retrying

log = logging.getLogger(__name__)

BASE = "https://api.coinalyze.net/v1"
EXCHANGES = {"A": "binance", "6": "bybit"}      # Coinalyze exchange code -> our venue name
limiter = WeightLimiter(per_minute=36, burst=20)  # 10% under the 40/min limit
MAX_SYMBOLS = 20
STRIP = ("1000000", "10000", "1000", "100")      # 1000PEPE-style contract multipliers


def enabled() -> bool:
    return bool(settings.coinalyze_api_key)


@retrying
async def _get(path: str, **params) -> list:
    n = len(str(params.get("symbols", "")).split(",")) if "symbols" in params else 1
    await limiter.acquire(n)
    r = await client().get(f"{BASE}/{path}", params={"api_key": settings.coinalyze_api_key} | params)
    if r.status_code == 429:
        await asyncio.sleep(float(r.headers.get("Retry-After", "10")))
    raise_for_status(r)                         # the key is in the URL: keep it out of the error text
    return r.json()


async def perp_symbols(bases: set[str]) -> dict[str, tuple[str, str, float]]:
    """-> {coinalyze symbol: (venue, base, multiplier)}: the USDT-margined perp per base per venue."""
    out = {}
    for m in await _get("future-markets"):
        venue = EXCHANGES.get(m["exchange"])
        if not venue or not m["is_perpetual"] or m["quote_asset"] != "USDT" or m.get("margined") != "STABLE":
            continue
        raw = m["base_asset"].upper()
        base, mult = raw, 1.0
        for p in STRIP:
            if raw.startswith(p) and raw[len(p):] in bases:
                base, mult = raw[len(p):], float(p)
                break
        if base in bases:
            out.setdefault(m["symbol"], (venue, base, mult))
    return out


async def batched(path: str, symbols: list[str], **params) -> list:
    out = []
    for i in range(0, len(symbols), MAX_SYMBOLS):
        out += await _get(path, symbols=",".join(symbols[i:i + MAX_SYMBOLS]), **params)
    return out
