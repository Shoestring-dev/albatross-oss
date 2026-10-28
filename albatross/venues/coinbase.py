"""Coinbase Exchange public REST (no key). US spot flow; basis for the Coinbase premium signal."""

from datetime import UTC, datetime

from albatross.http import WeightLimiter, client, retrying

BASE = "https://api.exchange.coinbase.com"
VENUE = "coinbase"

limiter = WeightLimiter(per_minute=240)

GRANULARITY = {"5m": 300, "1h": 3600, "1d": 86400}


@retrying
async def _get(path: str, params: dict | None = None):
    await limiter.acquire()
    r = await client().get(BASE + path, params=params)
    r.raise_for_status()
    return r.json()


async def usd_products() -> list[dict]:
    """Online USD spot pairs: [{'id': 'BTC-USD', 'symbol': 'BTC'}, ...]"""
    data = await _get("/products")
    return [
        {"id": p["id"], "symbol": p["base_currency"].upper()}
        for p in data
        if p["quote_currency"] == "USD" and p["status"] == "online" and not p.get("trading_disabled")
    ]


async def candles(product_id: str, tf: str, start: datetime, end: datetime) -> list[list]:
    """Max 300 candles per call. Rows: [time, low, high, open, close, volume]."""
    return await _get(
        f"/products/{product_id}/candles",
        {"granularity": GRANULARITY[tf], "start": start.isoformat(), "end": end.isoformat()},
    )


def candle_row(c: list, symbol: str, tf: str) -> tuple:
    return (
        VENUE, "spot", symbol, tf,
        datetime.fromtimestamp(int(c[0]), UTC),
        float(c[3]), float(c[2]), float(c[1]), float(c[4]), float(c[5]), None,
    )
