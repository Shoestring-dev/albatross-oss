"""DexScreener (free, no key): every pool of a token, and pool-level price, liquidity and market cap.

Limits (DexScreener OpenAPI spec behind docs.dexscreener.com/api/reference, checked 2026-09-29): /tokens/v1,
/token-pairs/v1 and /latest/dex/pairs allow 300 requests/min; /tokens/v1 and /latest/dex/pairs take up to 30
comma-separated addresses. We stay at 120/min.

Measured 2026-09-29: responses are CDN-cached for 30 s (Cache-Control: max-age=30, an Age header on hits), so a
quote's source time is receipt time minus Age. /tokens/v1 returns ONE pair per token (DexScreener's pick), which
is why discovery uses /token-pairs/v1. pump.fun bonding-curve pairs come back with priceUsd, liquidity and
marketCap all null. EVM addresses come back checksummed (the Telegram feed stores them lowercase).
"""

from datetime import UTC, datetime, timedelta

from albatross.http import WeightLimiter, client, retrying

BASE = "https://api.dexscreener.com"
MAX_ADDRESSES = 30
limiter = WeightLimiter(per_minute=120, burst=10)


@retrying
async def _get(path: str) -> tuple[object, datetime]:
    await limiter.acquire()
    r = await client().get(BASE + path)
    r.raise_for_status()
    age = float(r.headers.get("age") or 0)
    return r.json(), datetime.now(UTC) - timedelta(seconds=age)


async def token_pairs(chain: str, token: str) -> tuple[list[dict], datetime]:
    """All pools of one token, on either side."""
    data, ts = await _get(f"/token-pairs/v1/{chain}/{token}")
    return data or [], ts


async def tokens(chain: str, addresses: list[str]) -> tuple[list[dict], datetime]:
    """One pair per token that DexScreener knows on this chain (up to 30 tokens)."""
    data, ts = await _get(f"/tokens/v1/{chain}/{','.join(addresses[:MAX_ADDRESSES])}")
    return data or [], ts


async def pairs(chain: str, pools: list[str]) -> tuple[list[dict], datetime]:
    """Current state of up to 30 pools."""
    data, ts = await _get(f"/latest/dex/pairs/{chain}/{','.join(pools[:MAX_ADDRESSES])}")
    return (data or {}).get("pairs") or [], ts
