"""Hourly Binance/Bybit perp metrics for Tier 1+2 via Coinalyze -> cex_derivs_1h + cex_funding.

Each run re-pulls the last 25 hours (same call cost as one hour on Coinalyze), so a missed run
heals itself and the in-progress hour is overwritten once it completes. ~800 calls/run at the
36/min limiter, i.e. ~23 minutes; scheduled at :01 so the Flow analyst (:30) sees fresh data.
"""

import logging
import time
from datetime import UTC, datetime

from albatross import db
from albatross.venues import coinalyze

log = logging.getLogger(__name__)


async def sync(tiers: tuple[int, ...] = (1, 2)) -> int:
    if not coinalyze.enabled():
        return 0
    pool = await db.pool()
    bases = {r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM assets WHERE active AND tier = ANY($1::smallint[])", list(tiers))}
    syms = await coinalyze.perp_symbols(bases)
    names = list(syms)
    now = int(time.time())
    window = {"interval": "1hour", "from": now - 26 * 3600, "to": now}

    rows: dict[tuple[str, str, int], dict] = {}

    def put(sym: str, t: int, **vals) -> None:
        venue, base, _ = syms[sym]
        rows.setdefault((venue, base, t), {}).update(vals)

    for item in await coinalyze.batched("open-interest-history", names, convert_to_usd="true", **window):
        for h in item.get("history", []):
            put(item["symbol"], h["t"], oi_usd=h.get("c"))
    for item in await coinalyze.batched("liquidation-history", names, convert_to_usd="true", **window):
        for h in item.get("history", []):
            put(item["symbol"], h["t"], liq_long_usd=h.get("l"), liq_short_usd=h.get("s"))
    for item in await coinalyze.batched("long-short-ratio-history", names, **window):
        for h in item.get("history", []):
            put(item["symbol"], h["t"], long_pct=h.get("l"))
    for item in await coinalyze.batched("ohlcv-history", names, **window):
        mult = syms[item["symbol"]][2]
        for h in item.get("history", []):
            # 1000PEPE-style contracts: volume is in units of `mult` tokens, price is per `mult` tokens.
            put(item["symbol"], h["t"], volume=(h.get("v") or 0) * mult, buy_volume=(h.get("bv") or 0) * mult,
                close=(h["c"] / mult) if h.get("c") else None)

    cols = ["oi_usd", "liq_long_usd", "liq_short_usd", "long_pct", "volume", "buy_volume", "close"]
    await db.upsert(
        "cex_derivs_1h", ["venue", "symbol", "ts", *cols],
        [(v, s, datetime.fromtimestamp(t, UTC), *(r.get(c) for c in cols)) for (v, s, t), r in rows.items()],
        ["venue", "symbol", "ts"],
    )

    funding: dict[str, dict] = {}
    for item in await coinalyze.batched("funding-rate", names):
        funding.setdefault(item["symbol"], {})["f"] = item.get("value")
    for item in await coinalyze.batched("predicted-funding-rate", names):
        funding.setdefault(item["symbol"], {})["p"] = item.get("value")
    ts = datetime.now(UTC).replace(second=0, microsecond=0)
    await db.upsert(
        "cex_funding", ["venue", "symbol", "ts", "funding_pct", "predicted_pct"],
        [(syms[s][0], syms[s][1], ts, v.get("f"), v.get("p")) for s, v in funding.items()],
        ["venue", "symbol", "ts"],
    )
    return len(rows)
