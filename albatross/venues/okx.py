"""OKX public REST (no key). Used for perps that Hyperliquid doesn't list, and as a cross-check."""

from datetime import UTC, datetime

from albatross.http import WeightLimiter, client, retrying

BASE = "https://www.okx.com"
VENUE = "okx"

# Market-data endpoints allow ~20 req / 2s; stay far below.
limiter = WeightLimiter(per_minute=300)

BARS = {"5m": "5m", "1h": "1H", "1d": "1Dutc"}


@retrying
async def _get(path: str, params: dict) -> list:
    await limiter.acquire()
    r = await client().get(BASE + path, params=params)
    r.raise_for_status()
    body = r.json()
    if body.get("code") not in ("0", 0):
        raise RuntimeError(f"okx {path}: {body.get('msg')}")
    return body["data"]


async def usdt_swaps() -> list[dict]:
    """Live USDT-margined perpetuals: [{'instId': 'BTC-USDT-SWAP', 'symbol': 'BTC'}, ...]"""
    data = await _get("/api/v5/public/instruments", {"instType": "SWAP"})
    return [
        {"instId": d["instId"], "symbol": d["ctValCcy"].upper()}
        for d in data
        # instCategory 1 = crypto; OKX also lists tokenised stocks (3) and commodities (4) as swaps.
        if d["settleCcy"] == "USDT" and d["state"] == "live" and d.get("instCategory") == "1"
    ]


async def candles(inst_id: str, tf: str, after_ms: int | None = None, limit: int = 300) -> list[list]:
    """Newest-first candles. `after_ms` pages backwards (returns candles older than it)."""
    params = {"instId": inst_id, "bar": BARS[tf], "limit": str(limit)}
    path = "/api/v5/market/candles"
    if after_ms is not None:
        params["after"] = str(after_ms)
        path = "/api/v5/market/history-candles"
        params["limit"] = str(min(limit, 100))
    return await _get(path, params)


def candle_row(c: list, symbol: str, tf: str) -> tuple:
    # [ts, o, h, l, c, vol(contracts), volCcy(base), volCcyQuote, confirm]
    return (
        VENUE, "perp", symbol, tf,
        datetime.fromtimestamp(int(c[0]) / 1000, UTC),
        float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6]), None,
    )


async def announcements(ann_type: str) -> list[dict]:
    """Newest notices of one type (announcements-new-listings, announcements-delistings), first page:
    [{'title', 'url', 'pTime', 'businessPTime'}, ...]. OKX answers by the caller's region: from a US address
    these are OKX US's (en-us) notices."""
    data = await _get("/api/v5/support/announcements", {"annType": ann_type})
    return data[0]["details"] if data else []


@retrying
async def page(url: str) -> str:
    """An announcement's HTML: the listing timeline lives only in the article body."""
    await limiter.acquire()
    r = await client().get(url)
    r.raise_for_status()
    return r.text
