"""Derivatives snapshots and market-wide sentiment series."""

from datetime import UTC, datetime

from albatross import db
from albatross.http import client, retrying
from albatross.venues import hyperliquid


async def hyperliquid_contexts() -> int:
    """Funding, OI, mark/oracle, premium and volume for every HL perp — one call, run every minute."""
    ts = datetime.now(UTC).replace(second=0, microsecond=0)
    rows = []
    for perp, ctx in await hyperliquid.perps_with_contexts():
        if perp.delisted:
            continue
        m = perp.multiplier
        mark = float(ctx["markPx"]) / m if ctx.get("markPx") else None
        oi = float(ctx["openInterest"]) * m if ctx.get("openInterest") else None
        rows.append((
            hyperliquid.VENUE, perp.symbol, ts,
            float(ctx["funding"]) if ctx.get("funding") else None,
            oi,
            oi * mark if oi is not None and mark is not None else None,
            mark,
            float(ctx["oraclePx"]) / m if ctx.get("oraclePx") else None,
            float(ctx["premium"]) if ctx.get("premium") else None,
            float(ctx["dayNtlVlm"]) if ctx.get("dayNtlVlm") else None,
        ))
    return await db.upsert(
        "deriv_snapshots",
        ["venue", "symbol", "ts", "funding_rate", "open_interest", "oi_usd", "mark_px", "oracle_px",
         "premium", "day_volume_usd"],
        rows, ["venue", "symbol", "ts"],
    )


@retrying
async def _fng(limit: int) -> list[dict]:
    r = await client().get("https://api.alternative.me/fng/", params={"limit": limit})
    r.raise_for_status()
    return r.json()["data"]


async def fear_greed(limit: int = 3) -> int:
    """Crypto Fear & Greed index (daily). limit=0 returns full history for backfill."""
    rows = [
        ("alternative.me", "fear_greed", datetime.fromtimestamp(int(d["timestamp"]), UTC),
         float(d["value"]), None)
        for d in await _fng(limit)
    ]
    return await db.upsert("metric_series", ["source", "metric", "ts", "value", "meta"], rows,
                           ["source", "metric", "ts"])
